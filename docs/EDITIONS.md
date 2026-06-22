# Antigravity — Community vs Pro (selling guide)

## Product split

| Edition | Tabs | License | Distribution |
|---------|------|---------|--------------|
| **Community** | Tab1–Tab10 | MIT (`LICENSE` in dist) | Public GitHub / free zip |
| **Pro** | Tab11–Tab18 | Commercial (`antigravity_pro/LICENSE`) | Paid zip / private repo invite |

Pro is an **add-on package** — not a fork. Buyers install community (or your full dev tree stripped to community) then add `antigravity_pro`.

---

## Build Community release

```powershell
cd E:\ForwardBnrun\multitrategy
.\.venv\Scripts\python.exe scripts\build_community.py
```

Output: `dist/community/` (verified: compile + unit tests).

Create zip for upload:

```powershell
.\scripts\publish_community.ps1
```

Artifact: `dist/antigravity-community-YYYYMMDD.zip`

Pro zip for Gumroad:

```powershell
.\scripts\package_pro.ps1
```

Artifact: `dist/antigravity-pro-YYYYMMDD.zip`

Copy for listings: `docs/GUMROAD_PRO.md`, public README: `docs/README_COMMUNITY_PUBLIC.md`

---

## Public GitHub (Community)

Recommended layout:

1. **Repo A (public):** `antigravity-community` — contents of `dist/community/` only  
   - Never commit `antigravity_pro/`  
   - README points to Pro purchase link  

2. **Repo B (private):** `antigravity-pro` — only `antigravity_pro/` package  
   - Invite buyers after payment  
   - Tag releases `pro-v1.0.0` aligned with community `v1.0.0`

### Sync workflow

```text
main dev repo (multitrategy)
    │
    ├─► python scripts/build_community.py
    │       └─► push dist/community → public repo
    │
    └─► tag antigravity_pro/ → private repo release
```

---

## Selling Pro (Gumroad / Lemon Squeezy)

### What to deliver

1. **Community** — link to public GitHub OR include community zip (same as build)  
2. **Pro zip** — `antigravity_pro/` folder only (or full private repo access)  
3. **INSTALL.md** — `antigravity_pro/INSTALL.md`  
4. **LICENSE** — buyer accepts commercial terms  

### Suggested product page copy

- **Title:** Antigravity Pro — Tab11–Tab18 Strategy Pack  
- **Includes:** Volume pressure, spike breakout, 4H clones, momentum Tab17, Tab18 ultimate  
- **Requires:** Antigravity Community (free) + Python 3.11+ + Binance Futures account  
- **License:** Single operator / one business — no redistribution  

### After purchase (manual or webhook)

1. Email download link for `antigravity-pro-vX.zip`  
2. Optional: GitHub org invite to private `antigravity-pro` repo  
3. Support channel (Discord/email) for install help  

---

## Version alignment

| Community tag | Pro tag | Notes |
|---------------|---------|-------|
| `v1.0.0` | `pro-v1.0.0` | Same engine API; Pro extends config TABS |

Document in Pro README: *Requires Community >= v1.0.0*

---

## Security / leakage checklist

Before publishing community:

- [ ] `dist/community` has no `antigravity_pro/`  
- [ ] `config.py` has no Tab11–18 in `TAB_TIMEFRAMES`  
- [ ] `strategies.py` has no `evaluate_tab11_*` … `tab18_*`  
- [ ] Private repo / zip not linked from public README  

---

## Runtime detection

Dashboard WebSocket sends:

```json
{
  "premium_loaded": true,
  "edition": "pro",
  "premium_tabs": ["Tab11", "Tab12", "..."]
}
```

Community build shows an upsell banner until Pro is installed (`premium_loaded: true`).
