import re
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
# load_dotenv()
load_dotenv(override=True)

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

SIP_URI_REGEX = re.compile(r"^sip:[^\s@]+@[^@\s]+\.[^@\s]+$", re.IGNORECASE)
DIALOUT_SIP_URI_REGEX = re.compile(r"dialout=(sip:[^\s@=]+@[^@\s']+)", re.IGNORECASE)

# def extract_sip_uri(value: str) -> str | None:
#     """Return the SIP URI if valid, else None."""
#     value = value.strip()
#     return value if isinstance(value, str) and SIP_URI_REGEX.match(value) else None

def extract_sip_uri(value: str, regex) -> str | None:
    """Extracts SIP URI following 'dialout=' from a string."""
    match = DIALOUT_SIP_URI_REGEX.search(value.strip())
    return match.group(1) if match else None

def get_sip_uri_from_aliases(participant_aliases: list[str]) -> str | None:
    for alias in participant_aliases:
        if alias.lower().startswith("sip:"):
            return alias
    return None

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
    url = f"{settings.scheduler_api_url}encounter/"
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
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            logger.debug(f"Scheduler API response: {response.text}")
            return response.json().get("results", [])
        except httpx.HTTPError as e:
            logger.error(f"❌ Failed to fetch encounters: {e}")
            return []

async def get_starting_encounters_from_scheduler() -> list:
    logger.info("\U0001f4e5 Fetching encounters from scheduler...")
    token = await scheduler_token_mgr.get_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{settings.scheduler_api_url}encounter/"
    now = datetime.now(ZoneInfo("Australia/Sydney"))
    one_hour_later = now + timedelta(hours=1)

    params = {
        "start_time__gte": now.strftime("%H:%M:%S"),
        "start_time__lte": one_hour_later.strftime("%H:%M:%S"),
        "start_date": now.strftime("%Y-%m-%d")
    }

    logger.debug(f"Querying scheduler with params: {params}")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, params=params)
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

async def dial_out_from_conference(participant_sip_uri: str, participant_role:str, conference_alias:str, display_name: str, dry_run: bool = False) -> bool:
    #  must be a better way of doing this in bulk...

    token = await mgr_token_mgr.get_token()
    url = f"{settings.mgr_api_url}command/v1/participant/dial/"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "conference_alias": conference_alias,
        "destination": participant_sip_uri,
        "routing": "routing_rule",
        "remote_display_name": display_name,
        "role": participant_role,
        "system_location": "internal",
        }

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

async def get_participant_details(participant_id: int):
    token = await scheduler_token_mgr.get_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{settings.scheduler_api_url}participant/{participant_id}/"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            participant_data = response.json()
            logger.debug("👤 Participant %s details: %s", participant_id, participant_data)
            return participant_data
        except httpx.HTTPError as e:
            logger.error(f"❌ Failed to fetch participant: {e}")
            return []

async def get_roles():
    token = await scheduler_token_mgr.get_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{settings.scheduler_api_url}role/"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            roles = response.json().get("results", [])
            logger.debug("Got roles %s", roles)
            return roles
        except httpx.HTTPError as e:
            logger.error(f"❌ Failed to fetch participant: {e}")
            return []

def get_host_by_id(roles, target_id):
    host_value = next((role['host'] for role in roles if role['id'] == target_id), None)
    if host_value is True:
        return 'chair'
    elif host_value is False:
        return 'guest'
    else:
        return None
               
async def schedule_starting_jobs(starting_encounters: list, dry_run: bool = False, scheduler=None):
    # token = await scheduler_token_mgr.get_token()

    for encounter in starting_encounters:
        if not encounter.get("start_time") or not encounter.get("start_date") or not encounter.get("vmr"):
            continue

        tz = ZoneInfo(encounter.get("timezone") or "Australia/Sydney")
        start_time = datetime.strptime(
            f"{encounter['start_date']} {encounter['start_time']}",
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=tz)

        vmr_name = encounter["vmr"]
        encounter_name = encounter["name"]
        encounter_id = encounter["id"]

        """
        {"count":1,"next":null,"previous":null,"results":[{"id":"4c9dd270-2843-4a6e-bcce-678ed80e5401","name":"test future","vmr":"test2","description":"","start_date":"2025-04-30","all_day":false,"start_time":"19:50:00","end_time":"20:10:00",
        "timezone":"Australia/Sydney","end_date":null,"recurrence":null,"main_language":null,"adhoc_guest_breakout_id":"70883623-dc93-4b66-b6fd-b2a929fd2a09","enable_chat":true,"enable_overlay_text":true,"guests_can_present":true,"mute_all_guests":false,
        "theme":null,"breakout_room_theme":null,"pinning_config":"","participant_limit":null,"view":6,"breakout_rooms_mode":"AUTOMATIC","rtmp_streams":[],"mail_sequence":11,"encounter_aliases":[],"encounter_participants":[{"id":30,"participant":2,"role":1,
        "language":null,"paired_participant":null,"participant_aliases":["sip:737957293@pextest.com","737957293"],"short_alias":"737957293","long_alias":"b3d05594-74ef-4734-bddb-ab37f3e00cd9"},{"id":31,"participant":1,"role":2,"language":null,"paired_participant":null,
        "participant_aliases":["sip:309672685@pextest.com","309672685"],"short_alias":"309672685","long_alias":"0fe67bd1-1b6d-45df-9243-4e89200d50a0"}],"breakout_rooms":[{"id":3,"name":"Waiting Room - Presecution","role":2,
        "breakout_id":"2dff6d11-3cfa-41cd-bce3-4b7bb3ae19d4","locked":false}],"created_by":1,"access_groups":[]}]}
        """
        logger.info("🔍 Encounter '%s' starting at %s with encounter_id %s and vmr_name %s", encounter_name, start_time, encounter_id, vmr_name)
        roles = await get_roles()
        for p in encounter.get("encounter_participants", []):
            # logger.info("  - Participant id: %d, Participant alias: %s", p.get("participant"), p.get("participant_aliases", []))
            participant_id = p.get("participant")
            participant_role_value = p.get("role")
            participant_role = get_host_by_id(roles, participant_role_value)
            participant_data = await get_participant_details(participant_id)
            # logger.debug("participant_data: %s", participant_data)

            participant_sip_uri = extract_sip_uri(participant_data["description"], regex=DIALOUT_SIP_URI_REGEX)
            if participant_sip_uri:
                conference_alias = get_sip_uri_from_aliases(p["participant_aliases"])
                logger.info("📡 Extracted endpoint SIP URI from description: %s, participant role: %s, conference_alias: %s dialing out from encounter_name: %s", participant_sip_uri, participant_role, conference_alias, encounter_name)
                job_id = f"dialout-{participant_sip_uri}"
                existing_job = scheduler.get_job(job_id)

                if existing_job:
                    scheduled_time = existing_job.trigger.run_date
                    if scheduled_time != start_time:
                        logger.info(f"🔁 Rescheduling dialout out job for '{participant_sip_uri}' from VMR '{vmr_name}' to new start time '{start_time}'")
                        scheduler.remove_job(job_id)
                    else:
                        logger.debug(f"⏳ Dialout out job for '{participant_sip_uri}' from '{vmr_name}' already scheduled at {start_time}, skipping...")
                        continue

                logger.info(f"⏰ Scheduling dialout for '{participant_sip_uri}' from VMR '{vmr_name}' at {start_time}...")
                scheduler.add_job(
                    dial_out_from_conference,
                    trigger=DateTrigger(run_date=start_time),
                    args=[participant_sip_uri, participant_role, conference_alias, participant_data["display_name"]],
                    # kwargs={"dry_run": dry_run},
                    id=job_id,
                    replace_existing=True
                )

            else:
                # logger.info(f"✅ No active conference for VMR '{vmr_name}' at scheduling time.")
                return

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
            starting_encounters = await get_starting_encounters_from_scheduler()
            await schedule_disconnect_jobs(disconnect_encounters, dry_run=dry_run_flag, scheduler=scheduler)
            await schedule_starting_jobs(starting_encounters, dry_run=dry_run_flag, scheduler=scheduler)
            await asyncio.sleep(settings.poll_interval)

    try:
        asyncio.run(poll_and_schedule())
    except KeyboardInterrupt:
        logger.info("✅ Gracefully shutting down on keyboard interrupt.")






