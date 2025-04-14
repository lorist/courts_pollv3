# VMR Cleanup Scheduler

This Python application monitors scheduled video encounters (VMRs) and ensures any lingering active conferences on the Pexip management node are disconnected at the scheduled end time.

## Features

- Polls a scheduling API for encounters that end within the next hour.
- Uses OAuth2 client assertion JWT to authenticate with both the scheduler and Pexip APIs.
- Schedules a disconnect job at each encounter's `end_time` using APScheduler.
- Automatically reschedules if the `end_time` changes between polls.
- Supports dry-run mode (`--dry-run`) for testing.
- Gracefully handles keyboard interrupts.
- Configurable polling interval.
- Rotating logs with timestamps.

## Requirements

- Python 3.10+
- Dependencies from `requirements.txt`

## Installation

1. Clone this repository.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt

3. Create a .env file

    ```
    POLL_INTERVAL=60
    SCHEDULER_API_URL=https://your-scheduler/api/encounter/
    SCHEDULER_OAUTH_TOKEN_URL=https://your-scheduler/oauth/token/
    SCHEDULER_ISSUER=your-scheduler-client-id
    SCHEDULER_PRIVATE_KEY_PATH=path/to/private_key.pem

    MGR_API_URL=https://your-pexip/api/admin/
    MGR_OAUTH_TOKEN_URL=https://your-pexip/oauth/token/
    MGR_ISSUER=your-mgr-client-id
    MGR_PRIVATE_KEY_PATH=path/to/mgr_private_key.pem


4. Create a pem private key file for both the web scheduler and the Pexip Infinity Management node so that the script can talk to the respective APIs using OAuth2 auth.

5. Run: `python vmr_cleanup.py`

Or do a dry run without actually talking to the Infinity API: `python vmr_cleanup.py --dry-run`

6. Docker: `docker compose up --build`