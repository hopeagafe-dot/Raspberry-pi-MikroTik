@echo off
pyinstaller --onefile download_manual_archives.py
pyinstaller --onefile download_monthly_archives.py
pyinstaller --onefile download_daily_archives.py
pyinstaller --onefile download_mcepi_data.py
pyinstaller --onefile download_home_mce.py
pyinstaller --onefile download_Templates.py
echo.
echo ===== 빌드 완료 =====
pause
```

---

**완료 후 결과물 위치**

빌드가 끝나면 `dist\` 폴더 안에 `.exe` 파일들이 생성됩니다.
```
dist/
├── download_manual_archives.exe
├── download_monthly_archives.exe
├── download_daily_archives.exe
├── download_mcepi_data.exe
├── download_home_mce.exe
└── download_Templates.exe