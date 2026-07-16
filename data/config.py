from environs import Env

# using the environs library
env = Env()
env.read_env()

# We read the following from the .env file
BOT_TOKEN = env.str("BOT_TOKEN")  # Bot token
ADMINS = env.list("ADMINS")  # list of admins
IP = env.str("ip")  # The host ip address

# PostgreSQL
DB_USER = env.str("DB_USER")
DB_PASS = env.str("DB_PASS")
DB_NAME = env.str("DB_NAME")
DB_HOST = env.str("DB_HOST")

# E-POS API
EPOS_PHONE = env.str("EPOS_PHONE", "")
EPOS_PASSWORD = env.str("EPOS_PASSWORD", "")
EPOS_API_URL = env.str("EPOS_API_URL", "http://api.epos.uz")

# E-POS Management API (второй бэкенд, https://api.management.epos.uz)
EPOS_MGMT_API_URL = env.str("EPOS_MGMT_API_URL", "https://api.management.epos.uz")
EPOS_MGMT_PHONE = env.str("EPOS_MGMT_PHONE", "")
EPOS_MGMT_PASSWORD = env.str("EPOS_MGMT_PASSWORD", "")
EPOS_MGMT_OTP = env.str("EPOS_MGMT_OTP", "")
EPOS_MGMT_VERIFICATION_TOKEN = env.str("EPOS_MGMT_VERIFICATION_TOKEN", "")

# Telegram group to receive new-PDF / new-client notifications
PDF_GROUP_CHAT_ID = env.int("PDF_GROUP_CHAT_ID", 0)
