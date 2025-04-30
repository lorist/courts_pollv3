import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx
import jwt
import time
import logging
import sys
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# Load .env file
load_dotenv()

from logging.handlers import RotatingFileHandler

log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
file_handler = RotatingFileHandler("vmr_cleanup.log", maxBytes=100 * 1024 * 1024, backupCount=5)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.DEBUG)

logger = logging.getLogger("vmr_cleanup")
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)


# --- Settings ---
class Settings(BaseSettings):
    # general settings
    poll_interval: int = 60
    # Scheduler API settings
    scheduler_api_url: str
    scheduler_oauth_token_url: str
    scheduler_issuer: str
    scheduler_private_key_path: str

    # Management node API settings
    mgr_api_url: str
    mgr_oauth_token_url: str
    mgr_issuer: str
    mgr_private_key_path: str

    class Config:
        env_file = ".env"

settings = Settings()

# --- Token Manager ---
class TokenManager:
    def __init__(self, issuer: str, audience: str, private_key_path: str, token_url: str):
        self.issuer = issuer
        self.audience = audience
        self.private_key_path = private_key_path
        self.token_url = token_url
        self._cached_token = None
        self._expires_at = 0

    def _generate_client_assertion(self):
        with open(self.private_key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
        now = int(time.time())
        payload = {
            "iss": self.issuer,
            "sub": self.issuer,
            "aud": self.audience,
            "iat": now,
            "exp": now + 3600,
            "jti": str(now)
        }
        return jwt.encode(payload, private_key, algorithm="ES256")

    async def get_token(self):
        now = time.time()
        if self._cached_token and now < self._expires_at - 60:
            return self._cached_token

        logger.info(f"\U0001f510 Fetching new token for issuer: {self.issuer}")
        assertion = self._generate_client_assertion()
        async with httpx.AsyncClient() as client:
            r = await client.post(self.token_url, data={
                "grant_type": "client_credentials",
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
            })
            r.raise_for_status()
            token_data = r.json()
            self._cached_token = token_data["access_token"]
            self._expires_at = now + token_data.get("expires_in", 3600)
            return self._cached_token

# Instantiate Token Managers
scheduler_token_mgr = TokenManager(
    issuer=settings.scheduler_issuer,
    audience=settings.scheduler_oauth_token_url,
    private_key_path=settings.scheduler_private_key_path,
    token_url=settings.scheduler_oauth_token_url,
)

mgr_token_mgr = TokenManager(
    issuer=settings.mgr_issuer,
    audience=settings.mgr_oauth_token_url,
    private_key_path=settings.mgr_private_key_path,
    token_url=settings.mgr_oauth_token_url,
)

# --- Scheduler API ---
async def get_disconnect_encounters_from_scheduler() -> list:
    logger.info("\U0001f4e5 Fetching encounters from scheduler...")
    token = await scheduler_token_mgr.get_token()
    headers = {"Authorization": f"Bearer {token}"}

    now = datetime.now(ZoneInfo("Australia/Sydney"))
    one_hour_later = now + timedelta(hours=1)

    params = {
        "end_time__gte": now.strftime("%H:%M:%S"),
        "end_time__lte": one_hour_later.strftime("%H:%M:%S"),
        "start_date": now.strftime("%Y-%m-%d")
    }

    logger.debug(f"Querying scheduler with params: {params}")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(settings.scheduler_api_url, headers=headers, params=params)
            response.raise_for_status()
            logger.debug(f"Scheduler API response: {response.text}")
            return response.json().get("results", [])
        except httpx.HTTPError as e:
            logger.error(f"❌ Failed to fetch encounters: {e}")
            return []

# --- Pexip Management API ---
async def get_conference_id_by_name(name: str, token: str) -> str | None:
    url = f"{settings.mgr_api_url}status/v1/conference/?name={name}"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        logger.debug(f"GET {url}")
        r = await client.get(url, headers=headers)
        logger.debug(f"Response status: {r.status_code}")
        logger.debug(f"Response body: {r.text}")
        if r.status_code == 200:
            data = r.json()
            if data.get("objects"):
                conf_id = data["objects"][0].get("id")
                logger.debug(f"Extracted conference ID: {conf_id}")
                return conf_id
    return None

async def disconnect_conference(conference_id: str, token: str, dry_run: bool = False) -> bool:
    url = f"{settings.mgr_api_url}command/v1/conference/disconnect/"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {"conference_id": conference_id}

    if dry_run:
        logger.info(f"[Dry-run] Would POST to {url} with payload: {payload}")
        return True

    async with httpx.AsyncClient() as client:
        logger.debug(f"POST {url} with payload: {payload}")
        r = await client.post(url, headers=headers, json=payload)
        logger.debug(f"Response status: {r.status_code}")
        logger.debug(f"Response body: {r.text}")
        return r.status_code == 200

# --- Scheduler Job Setup ---
async def schedule_disconnect_jobs(disconnect_encounters: list, dry_run: bool = False, scheduler=None):
    token = await mgr_token_mgr.get_token()

    for encounter in disconnect_encounters:
        if not encounter.get("end_time") or not encounter.get("start_date") or not encounter.get("vmr"):
            continue

        tz = ZoneInfo(encounter.get("timezone") or "Australia/Sydney")
        end_dt = datetime.strptime(
            f"{encounter['start_date']} {encounter['end_time']}",
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=tz)

        vmr_name = encounter["vmr"]
        conf_id = await get_conference_id_by_name(vmr_name, token)
        if conf_id:
            job_id = f"disconnect-{conf_id}"
            existing_job = scheduler.get_job(job_id)

            if existing_job:
                scheduled_time = existing_job.trigger.run_date
                if scheduled_time != end_dt:
                    logger.info(f"🔁 Rescheduling job for VMR '{vmr_name}' to new end time {end_dt}")
                    scheduler.remove_job(job_id)
                else:
                    logger.debug(f"⏳ Job for VMR '{vmr_name}' already scheduled at {end_dt}, skipping...")
                    continue

            logger.info(f"⏰ Scheduling disconnect for VMR '{vmr_name}' at {end_dt}...")
            scheduler.add_job(
                disconnect_conference,
                trigger=DateTrigger(run_date=end_dt),
                args=[conf_id, token],
                kwargs={"dry_run": dry_run},
                id=job_id,
                replace_existing=True
            )

        else:
            logger.info(f"✅ No active conference for VMR '{vmr_name}' at scheduling time.")

# --- Entry Point ---
if __name__ == "__main__":
    # dry-run will find the conferences but not actually disconnect 
    dry_run_flag = "--dry-run" in sys.argv
    # # dial-out will dial out to participants with sip:<uri> in the participant description 
    # dial_out_flag = "--dial-out" in sys.argv

    async def poll_and_schedule():
        scheduler = AsyncIOScheduler()
        scheduler.start()

        while True:
            disconnect_encounters = await get_disconnect_encounters_from_scheduler()
            await schedule_disconnect_jobs(disconnect_encounters, dry_run=dry_run_flag, scheduler=scheduler)
            await asyncio.sleep(settings.poll_interval)

    try:
        asyncio.run(poll_and_schedule())
    except KeyboardInterrupt:
        logger.info("✅ Gracefully shutting down on keyboard interrupt.")