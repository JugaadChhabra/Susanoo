"""Create fresh local deployment secrets once; never rotate an existing install."""

import secrets
from paths import STATE, ensure_private_state
from susanoo_runner.local_security import secure_local_path

ensure_private_state()
destination = STATE / ".env"
with destination.open("x", encoding="utf-8") as stream:
    secure_local_path(destination)
    values = {
        "COMPOSE_PROJECT_NAME": "susanoo-local-sim",
        "NEXT_PUBLIC_APP_URL": "http://localhost:3010",
        "POSTGRES_DB": "simstudio",
    }
    for name in ["POSTGRES_PASSWORD", "BETTER_AUTH_SECRET", "ENCRYPTION_KEY", "INTERNAL_API_SECRET", "CRON_SECRET"]:
        values[name] = secrets.token_hex(32)
    stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
print(f"Created private local configuration at {destination}; values not displayed.")
