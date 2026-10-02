@echo off
cd /d "%~dp0"
echo Syncing Shopify, Wise and product costs...
python sync.py
echo.
echo Opening dashboard at http://localhost:8501  (close this window to stop it)
python -m streamlit run dashboard/app.py --server.port 8501
