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

def process_data(df, vat_frequency='2M', vat_basis='accrual', start_date=None, end_date=None):
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

    summary = df.groupby('VAT Period').agg({
        'Gross (EUR)': 'sum', 'Tax (EUR)': 'sum', 'Net (EUR)': 'sum',
        'Unpaid/Unreceived': lambda x: (x == 'Yes').sum(),
        'Bad Debt Risk': lambda x: (x == 'Yes').sum()
    }).reset_index()

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
        max_period = summary['VAT Period'].max()
        if not pd.isna(max_period):
            # Simplified check for partial
            if df['Invoice Date'].max() < pd.to_datetime(end_date):
                st.warning("Partial period data detected (e.g., 2025 Jan-Aug)—review manually.")

    return df, summary

def check_disclosures(summary, filed_vat_df):
    if filed_vat_df is None:
        return None
    filed_vat_df['VAT Period'] = filed_vat_df['VAT Period'].astype(str)
    summary['VAT Period'] = summary['VAT Period'].astype(str)
    merged = summary.merge(filed_vat_df, on='VAT Period', suffixes=('_calc', '_filed'), how='left')
    merged['Discrepancy'] = merged['Tax (EUR)_calc'] - merged.get('Tax (EUR)_filed', 0)
    if (merged['Discrepancy'] != 0).any():
        liability = merged['Discrepancy'].sum()
        interest_rate = 0.000219
        days_late = (datetime.now() - datetime(2023, 1, 1)).days
        interest = liability * interest_rate * days_late
        st.warning(f"Discrepancy detected! Potential prompted qualifying disclosure needed before 24/10/2025.")
        st.write(f"Estimated Liability: €{liability:.2f} + Interest: €{interest:.2f}")
        disclosure_df = pd.DataFrame({
            'Details': ['Submit via ROS/MyEnquiries.', 'Include all VAT for scoped periods (2023-2025).', 'Benefits: Reduced penalties (e.g., 10% for careless default), no publication/prosecution.']
        })
        return disclosure_df
    return None

# Streamlit UI
st.title("VAT Risk Review Tool for Small Irish Businesses")
st.write("Automate Revenue Appendix A exports, reconciliations, and disclosures for VAT reviews.")

col1, col2 = st.columns(2)
with col1:
    start_date = st.date_input("Start Date", value=datetime(2023, 1, 1))
    vat_frequency = st.selectbox("VAT Frequency", ["2M (Bimonthly)", "Q (Quarterly)", "4M (4-Monthly)", "6M", "Y", "M"], index=2)  # Default to 4M
    vat_frequency = vat_frequency.split()[0]
with col2:
    end_date = st.date_input("End Date", value=datetime(2025, 8, 31))
    vat_basis = st.radio("VAT Basis", ["accrual", "cash"])

uploaded_export = st.file_uploader("Upload Xero Export XLSX (e.g., Payable Invoice Summary)", type=["xlsx"])
uploaded_filed = st.file_uploader("Upload Filed VAT Returns XLSX (Optional, for Reconciliations)", type=["xlsx"])

if st.button("Process and Generate Exports"):
    if uploaded_export is None:
        st.error("Please upload a Xero export file to process.")
    else:
        if not uploaded_export.name.endswith('.xlsx'):
            st.error("Invalid file type. Please upload an XLSX file for Xero export (PDFs not supported for data processing).")
        else:
            try:
                df = pd.read_excel(uploaded_export, skiprows=4)
                df = df.dropna(how='all')
                df = df[~df.apply(lambda row: row.astype(str).str.contains('Total').any(), axis=1)]

                numeric_cols = ['Gross (EUR)', 'Tax (EUR)', 'Net (EUR)', 'Gross (Source)', 'Balance (Source)']
                for col in numeric_cols:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')

                processed_df, summary = process_data(df, vat_frequency, vat_basis, start_date, end_date)

                st.subheader("Processed Purchases Listings")
                st.dataframe(processed_df)

                st.subheader("VAT Summary per Period")
                st.dataframe(summary)

                filed_df = pd.read_excel(uploaded_filed) if uploaded_filed else None
                disclosure_df = check_disclosures(summary, filed_df)
                if disclosure_df is not None:
                    st.subheader("Disclosure Template")
                    st.dataframe(disclosure_df)

                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    processed_df.to_excel(writer, sheet_name='Item2_Purchases_Listings', index=False)
                    summary.to_excel(writer, sheet_name='Item3_VAT_Summary', index=False)
                    if disclosure_df is not None:
                        disclosure_df.to_excel(writer, sheet_name='Disclosure_Template', index=False)
                output.seek(0)
                st.download_button("Download Appendix A Exports", data=output, file_name="vat_review_exports.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            except ValueError as ve:
                st.error(f"Data error: {ve}")
            except Exception as e:
                st.error(f"Error processing file: {e}")

st.markdown("---")
st.write("Note: For full Xero API integration, add credentials via Streamlit secrets. This tool is for guidance; consult Revenue for official advice.")