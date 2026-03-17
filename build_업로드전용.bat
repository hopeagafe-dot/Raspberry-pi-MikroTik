@echo off
pyinstaller --onefile upload_all_user_graph.py
pyinstaller --onefile upload_archive_billing.py
pyinstaller --onefile upload_billing_date.py
pyinstaller --onefile upload_manager.py
pyinstaller --onefile upload_mce_admin_api.py
pyinstaller --onefile upload_mce_usage_collector.py
echo.
echo ===== 빌드 완료 =====
pause
```

---

**완료 후 결과물 위치**

빌드가 끝나면 `dist\` 폴더 안에 `.exe` 파일들이 생성됩니다.
```
dist/
├── upload_all_user_graph.exe
├── upload_archive_billing.py
├── upload_billing_date.py
├── upload_manager.py
├── upload_mce_admin_api.py
└── upload_mce_usage_collector.py