"""Gemeinsame Testumgebung – wird vor allen Testdateien geladen."""
import os
import tempfile

os.environ["PF_DATA_DIR"] = tempfile.mkdtemp()
os.environ["PF_ADMIN_PASSWORD"] = "admin-passwort"
os.environ["PF_PUBLIC_URL"] = "https://mail.example.de"
os.environ["PF_SCHEDULER"] = "0"
os.environ["PF_UPDATE_CHECK"] = "0"
os.environ["PF_SELF_RESTART"] = "0"
