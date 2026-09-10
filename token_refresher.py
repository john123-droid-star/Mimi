# token_refresher.py
import httpx
import asyncio
import json
import base64
import os
from typing import Tuple, List, Dict
from google.protobuf import json_format, message
from Crypto.Cipher import AES

import freefire_pb2  # REQUIRED in repo root

# ── constants ──
MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==')
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')
RELEASEVERSION = "OB54"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"

ACCOUNTS_FILE = "accounts.json"

# ── global in-memory token cache (persists while Vercel function is warm) ──
TOKEN_CACHE = {
    "IND": [],
    "BR": [],
    "BD": [],
}


# ─────────── crypto helpers ───────────
def _pad(text: bytes) -> bytes:
    pl = AES.block_size - (len(text) % AES.block_size)
    return text + bytes([pl] * pl)


def _encrypt(plaintext: bytes) -> bytes:
    aes = AES.new(MAIN_KEY, AES.MODE_CBC, MAIN_IV)
    return aes.encrypt(_pad(plaintext))


async def _json_to_proto(json_data: str, proto_message: message.Message) -> bytes:
    json_format.ParseDict(json.loads(json_data), proto_message)
    return proto_message.SerializeToString()


def _decode_protobuf(data: bytes, message_type: message.Message) -> message.Message:
    m = message_type()
    m.ParseFromString(data)
    return m


# ─────────── auth ───────────
async def _get_access_token(uid: str, password: str) -> Tuple[str, str]:
    url = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
    payload = (
        f"uid={uid}&password={password}&response_type=token&client_type=2"
        "&client_secret=2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"
        "&client_id=100067"
    )
    headers = {
        "User-Agent": USERAGENT,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, data=payload, headers=headers)
        d = r.json()
        return d.get("access_token", "0"), d.get("open_id", "0")


async def create_jwt(uid: str, password: str) -> Tuple[str, str, str]:
    access_token, open_id = await _get_access_token(uid, password)
    if access_token == "0":
        raise ValueError(f"access_token failed for uid={uid}")

    json_data = json.dumps({
        "open_id": open_id,
        "open_id_type": "4",
        "login_token": access_token,
        "orign_platform_type": "4",
    })
    encoded = await _json_to_proto(json_data, freefire_pb2.LoginReq())
    payload = _encrypt(encoded)

    url = "https://loginbp.ggblueshark.com/MajorLogin"
    headers = {
        "User-Agent": USERAGENT,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/octet-stream",
        "Expect": "100-continue",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": RELEASEVERSION,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, data=payload, headers=headers)
        msg = json.loads(
            json_format.MessageToJson(_decode_protobuf(r.content, freefire_pb2.LoginRes))
        )
        token = msg.get("token", "0")
        region = msg.get("lockRegion", "0")
        server_url = msg.get("serverUrl", "0")
        if token == "0":
            raise ValueError(f"JWT failed for uid={uid}")
        return token, region, server_url


# ─────────── orchestration ───────────
async def refresh_one(account: Dict) -> Dict:
    """Refresh a single account → returns dict or None."""
    uid = str(account.get("uid", "")).strip()
    pwd = str(account.get("password", "")).strip()
    region = str(account.get("region", "IND")).upper()
    if not uid or not pwd:
        return None
    try:
        token, lock_region, _ = await create_jwt(uid, pwd)
        return {"uid": uid, "region": region, "token": token, "lock_region": lock_region}
    except Exception as e:
        print(f"[refresher] ✗ uid={uid} region={region} → {e}")
        return None


def _write_token_file(region: str, entries: List[Dict]):
    """Write tokens to the appropriate token_*.json file (best effort)."""
    if region in ("BR", "US", "SAC", "NA"):
        target = "token_br.json"
    elif region == "IND":
        target = "token_ind.json"
    else:
        target = "token_bd.json"

    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=4)
        print(f"[refresher] ✓ wrote {len(entries)} tokens → {target}")
    except Exception as e:
        # Vercel read-only FS — this is expected, ignore
        print(f"[refresher] disk write skipped ({target}): {e}")


async def refresh_all() -> dict:
    """Refresh every account and update in-memory + files."""
    if not os.path.exists(ACCOUNTS_FILE):
        return {"error": f"{ACCOUNTS_FILE} not found"}

    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)
    if not isinstance(accounts, list) or not accounts:
        return {"error": "accounts.json must be a non-empty list"}

    results = await asyncio.gather(*(refresh_one(a) for a in accounts))
    ok = [r for r in results if r]

    # clear cache
    for k in TOKEN_CACHE:
        TOKEN_CACHE[k] = []

    # fill cache by region
    for r in ok:
        region = r["region"]
        if region in ("BR", "US", "SAC", "NA"):
            TOKEN_CACHE["BR"].append({"token": r["token"]})
        elif region == "IND":
            TOKEN_CACHE["IND"].append({"token": r["token"]})
        else:
            TOKEN_CACHE["BD"].append({"token": r["token"]})

    # try to write to disk (Vercel: silently fails; Termux: works)
    try:
        _write_token_file("IND", TOKEN_CACHE["IND"])
        _write_token_file("BR", TOKEN_CACHE["BR"])
        _write_token_file("BD", TOKEN_CACHE["BD"])
    except Exception as e:
        print(f"[refresher] disk write skipped: {e}")

    return {
        "total": len(accounts),
        "success": len(ok),
        "failed": len(accounts) - len(ok),
        "cached": {k: len(v) for k, v in TOKEN_CACHE.items()},
    }
