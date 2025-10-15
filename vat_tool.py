import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import os
import io

def convert_date(value):
    try:
        date = pd.to_datetime(value, errors='coerce')
        if pd.notna(date):
            return date
        value = pd.to_numeric(value, errors='coerce')
        if pd.isna(value):
            return pd.NaT
        if 0 < value < 1e5:
            return pd.to_datetime('1899-12-30') + pd.to_timedelta(value, unit='D')
        elif 1e9 < value < 1e15:
            return pd.to_datetime(value, unit='s')
        elif value > 1e15:
            return pd.to_datetime(value, unit='ns')
        else:
            return pd.NaT
    except:
        return pd.NaT

def process_transactions(df, vat_frequency='2M', vat_basis='accrual', start_date=None, end_date=None, transaction_type='purchases'):
    # Convert dates
    date_cols = ['Invoice Date', 'Planned Date']
    for col in date_cols:
        if col in df.columns:
            df[col] = df[col].apply(convert_date)

    df = df.dropna(subset=['Invoice Date'])
    if df.empty:
        raise ValueError("No valid dates found in the file. Check the date columns.")

    if start_date:
        df = df[df['Invoice Date'] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df['Invoice Date'] <= pd.to_datetime(end_date)]

    # Custom grouping for '4M'
    if vat_frequency == '4M':
        df['Month Group'] = ((df['Invoice Date'].dt.month - 1) // 4) * 4 + 1
        df['Year'] = df['Invoice Date'].dt.year
        df['VAT Period'] = df['Year'].astype(str) + '-' + df['Month Group'].astype(str).str.zfill(2)
    else:
        freq_map = {
            'M': 'M', '2M': '2M', 'Q': 'Q', '6M': '6M', 'Y': 'Y'
        }
        freq = freq_map.get(vat_frequency, '2M')
        df['VAT Period'] = df['Invoice Date'].dt.to_period(freq)

    df['Unpaid/Unreceived'] = df['Status'].apply(lambda s: 'Yes' if s in ['Approved', 'Awaiting Payment'] else 'No')

    if vat_basis == 'cash':
        df = df[df['Status'] == 'Paid']
    else:
        df['Bad Debt Risk'] = df.apply(lambda row: 'Yes' if row['Unpaid/Unreceived'] == 'Yes' and (datetime.now() - row['Invoice Date'] > timedelta(days=180)) else 'No', axis=1)

    agg_dict = {
        'Gross (EUR)': 'sum',
        'Tax (EUR)': 'sum',
        'Net (EUR)': 'sum',
        'Unpaid/Unreceived': lambda x: (x == 'Yes').sum()
    }
    if vat_basis == 'accrual':
        agg_dict['Bad Debt Risk'] = lambda x: (x == 'Yes').sum()

    summary = df.groupby('VAT Period').agg(agg_dict).reset_index()

    # Round to nearest euro
    numeric_cols = ['Gross (EUR)', 'Tax (EUR)', 'Net (EUR)']
    summary[numeric_cols] = summary[numeric_cols].round(0)

    # Format VAT Period for readability
    if vat_frequency == '4M':
        def format_4m(period):
            year, month = period.split('-')
            month = int(month)
            start = datetime(int(year), month, 1)
            end = start + pd.DateOffset(months=4) - timedelta(days=1)
            return f"{start.strftime('%Y-%m')} to {end.strftime('%Y-%m')}"

        summary['VAT Period'] = summary['VAT Period'].apply(format_4m)
    else:
        summary['VAT Period'] = summary['VAT Period'].astype(str)

    # Partial period check
    if not summary.empty:
        if df['Invoice Date'].max() < pd.to_datetime(end_date):
            st.warning("Partial period data detected (e.g., 2025 Jan-Aug)—review manually.")

    return df, summary

def check_disclosures(summary_purchases, summary_sales, filed_vat_df):
    if filed_vat_df is None:
        return None, None

    # Ensure valid dataframes
    if summary_purchases.empty and summary_sales.empty:
        return None, None

    filed_vat_df['VAT Period'] = filed_vat_df['VAT Period'].astype(str)

    # Handle purchases reconciliation
    merged_purchases = pd.DataFrame() if summary_purchases.empty else summary_purchases.copy()
    if not summary_purchases.empty:
        merged_purchases = summary_purchases.merge(filed_vat_df[['VAT Period', 'T2 (EUR)']], on='VAT Period', how='left')
        if 'Tax (EUR)' in merged_purchases.columns:
            merged_purchases = merged_purchases.rename(columns={'Tax (EUR)': 'Tax (EUR)_calc_input'})
        else:
            merged_purchases['Tax (EUR)_calc_input'] = 0
        merged_purchases['Input Liability'] = (merged_purchases['Tax (EUR)_calc_input'] - merged_purchases.get('T2 (EUR)', 0)).round(0)
    else:
        merged_purchases['VAT Period'] = filed_vat_df['VAT Period']
        merged_purchases['Input Liability'] = 0

    # Handle sales reconciliation
    merged_sales = pd.DataFrame() if summary_sales.empty else summary_sales.copy()
    if not summary_sales.empty:
        merged_sales = summary_sales.merge(filed_vat_df[['VAT Period', 'T1 (EUR)']], on='VAT Period', how='left')
        if 'Tax (EUR)' in merged_sales.columns:
            merged_sales = merged_sales.rename(columns={'Tax (EUR)': 'Tax (EUR)_calc_output'})
        else:
            merged_sales['Tax (EUR)_calc_output'] = 0
        merged_sales['Output Liability'] = (merged_sales['Tax (EUR)_calc_output'] - merged_sales.get('T1 (EUR)', 0)).round(0)
    else:
        merged_sales['VAT Period'] = filed_vat_df['VAT Period']
        merged_sales['Output Liability'] = 0

    # Combine reconciliations
    merged = merged_purchases[['VAT Period', 'Tax (EUR)_calc_input', 'T2 (EUR)', 'Input Liability']].merge(
        merged_sales[['VAT Period', 'Tax (EUR)_calc_output', 'T1 (EUR)', 'Output Liability']],
        on='VAT Period', how='outer'
    )
    merged['Estimated Liability'] = merged['Output Liability'].fillna(0) + merged['Input Liability'].fillna(0)

    if (merged['Estimated Liability'] != 0).any():
        total_liability = merged['Estimated Liability'].sum()
        interest_rate = 0.000219
        days_late = (datetime.now() - datetime(2023, 1, 1)).days
        interest = total_liability * interest_rate * days_late
        st.warning(f"Discrepancy detected! Potential prompted qualifying disclosure needed before 24/10/2025.")
        st.write(f"Total Estimated Liability: €{total_liability:.0f} + Interest: €{interest:.0f}")
        disclosure_df = pd.DataFrame({
            'Details': ['Submit via ROS/MyEnquiries.', 'Include all VAT for scoped periods (2023-2025).', 'Benefits: Reduced penalties (e.g., 10% for careless default), no publication/prosecution.']
        })
        return merged, disclosure_df
    return merged, None

# Streamlit UI
st.title("VAT Risk Review Tool for Small Irish Businesses")
st.write("Automate Revenue Appendix A exports, reconciliations, and disclosures for VAT reviews.")

col1, col2 = st.columns(2)
with col1:
    start_date = st.date_input("Start Date", value=datetime(2023, 1, 1))
    vat_frequency = st.selectbox("VAT Frequency", ["2M (Bimonthly)", "Q (Quarterly)", "4M (4-Monthly)", "6M", "Y", "M"], index=2)
    vat_frequency = vat_frequency.split()[0]
with col2:
    end_date = st.date_input("End Date", value=datetime(2025, 8, 31))
    vat_basis = st.radio("VAT Basis", ["accrual", "cash"])

uploaded_purchases = st.file_uploader("Upload Xero Purchases XLSX (e.g., Payable Invoice Summary)", type=["xlsx"])
uploaded_sales = st.file_uploader("Upload Xero Sales XLSX (e.g., Receivable Invoice Summary)", type=["xlsx"])
uploaded_filed = st.file_uploader("Upload Filed VAT Returns XLSX (Optional, for Reconciliations)", type=["xlsx"])

if st.button("Process and Generate Exports"):
    if uploaded_purchases is None and uploaded_sales is None:
        st.error("Please upload at least one Xero export file to process.")
    else:
        purchases_df = None
        sales_df = None
        if uploaded_purchases:
            df = pd.read_excel(uploaded_purchases, skiprows=4)
            df = df.dropna(how='all')
            df = df[~df.apply(lambda row: row.astype(str).str.contains('Total').any(), axis=1)]
            numeric_cols = ['Gross (EUR)', 'Tax (EUR)', 'Net (EUR)', 'Gross (Source)', 'Balance (Source)']
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            purchases_df, purchases_summary = process_transactions(df, vat_frequency, vat_basis, start_date, end_date, 'purchases')
            st.subheader("Processed Purchases Listings")
            st.dataframe(purchases_df)
            st.subheader("Purchases VAT Summary per Period")
            st.dataframe(purchases_summary)
        if uploaded_sales:
            df = pd.read_excel(uploaded_sales, skiprows=4)
            df = df.dropna(how='all')
            df = df[~df.apply(lambda row: row.astype(str).str.contains('Total').any(), axis=1)]
            numeric_cols = ['Gross (EUR)', 'Tax (EUR)', 'Net (EUR)', 'Gross (Source)', 'Balance (Source)']
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            sales_df, sales_summary = process_transactions(df, vat_frequency, vat_basis, start_date, end_date, 'sales')
            st.subheader("Processed Sales Listings")
            st.dataframe(sales_df)
            st.subheader("Sales VAT Summary per Period")
            st.dataframe(sales_summary)

        filed_df = pd.read_excel(uploaded_filed) if uploaded_filed else None
        merged, disclosure_df = check_disclosures(purchases_summary if uploaded_purchases else pd.DataFrame(), sales_summary if uploaded_sales else pd.DataFrame(), filed_df)
        if merged is not None:
            st.subheader("Reconciliation Summary (per Period)")
            st.dataframe(merged[['VAT Period', 'Tax (EUR)_calc_input', 'T2 (EUR)', 'Input Liability', 'Tax (EUR)_calc_output', 'T1 (EUR)', 'Output Liability', 'Estimated Liability']])
        if disclosure_df is not None:
            st.subheader("Disclosure Template")
            st.dataframe(disclosure_df)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            if purchases_df is not None:
                purchases_df.to_excel(writer, sheet_name='Item2_Purchases_Listings', index=False)
            if purchases_summary is not None:
                purchases_summary.to_excel(writer, sheet_name='Item3_VAT_Summary_Purchases', index=False)
            if sales_df is not None:
                sales_df.to_excel(writer, sheet_name='Item1_Sales_Listings', index=False)
            if sales_summary is not None:
                sales_summary.to_excel(writer, sheet_name='Item3_VAT_Summary_Sales', index=False)
            if merged is not None:
                merged.to_excel(writer, sheet_name='Reconciliation_Summary', index=False)
            if disclosure_df is not None:
                disclosure_df.to_excel(writer, sheet_name='Disclosure_Template', index=False)
        output.seek(0)
        st.download_button("Download Appendix A Exports", data=output, file_name="vat_review_exports.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.markdown("---")
st.write("Note: For full Xero API integration, add credentials via Streamlit secrets. This tool is for guidance; consult Revenue for official advice.")
