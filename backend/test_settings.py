import sys
import os

# Add backend to path
sys.path.append(os.path.abspath("backend"))

try:
    from app.core.config import settings
    print("Settings loaded successfully!")
    print(f"CORS Origins: {settings.BACKEND_CORS_ORIGINS}")
    print(f"SuperAdmin Email: |{settings.FIRST_SUPERADMIN_EMAIL}|")
    print(f"SuperAdmin Password: |{settings.FIRST_SUPERADMIN_PASSWORD}|")
except Exception as e:
    print(f"Error loading settings: {e}")
    import traceback
    traceback.print_exc()
