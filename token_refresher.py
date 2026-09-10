# ── global in-memory token cache (persists while Vercel function is warm) ──
TOKEN_CACHE = {
    "IND": [],
    "BR": [],
    "BD": [],
}


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

    # try to write to disk (works locally/Termux, silently fails on Vercel)
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