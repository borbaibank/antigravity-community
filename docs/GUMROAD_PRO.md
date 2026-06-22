# Gumroad — Antigravity Pro (copy-paste)

ลิงก์ตั้งค่าแล้วสำหรับ **borbaibank** (จาก `git remote origin`)  
ถ้า Gumroad username ต่างจาก GitHub ให้แก้ `docs/PUBLISH_LINKS.md` แล้ว rebuild

---

## Product settings (แถบ Settings)

| Field | ค่า |
|-------|-----|
| **Name** | Antigravity Pro — Tab11–Tab18 Strategy Pack |
| **URL** | `antigravity-pro` |
| **Full URL** | https://borbaibank.gumroad.com/l/antigravity-pro |
| **Price** | $79 (ปรับได้) |
| **Currency** | USD |
| **Content type** | Digital product |

### Files to upload

```powershell
.\scripts\package_pro.ps1
```

อัปโหลด: `dist/antigravity-pro-YYYYMMDD.zip`

### Email receipt (วางใน Gumroad → Settings → Email)

```
ขอบคุณที่ซื้อ Antigravity Pro!

1. ติดตั้ง Antigravity Community ก่อน (ฟรี):
   https://github.com/borbaibank/antigravity-community

2. แตก zip แล้ววางโฟลเดอร์ antigravity_pro ข้าง server.py

3. รัน:
   pip install -e ./antigravity_pro
   python server.py

คู่มือ: เปิด INSTALL.md ใน zip
Support: https://github.com/borbaibank/antigravity-community/issues
```

---

## Product description (Description)

**Antigravity Pro** คือ add-on สำหรับ [Antigravity Community](https://github.com/borbaibank/antigravity-community) (ฟรี)  
ปลดล็อก **8 กลยุทธ์เพิ่ม (Tab11–Tab18)** — volume / momentum บน Binance USDT-M Futures

#### ได้อะไรบ้าง

- **Tab11** — Volume Pressure Proxy (1H)
- **Tab12** — Volume Spike Breakout (1H)
- **Tab13–16** — Tab9–12 logic บน 4H
- **Tab17** — Momentum Vol Pressure (1H, top 50 universe)
- **Tab18** — Vol ultimate (1H)

- Source code Python — self-hosted
- ติดตั้ง `pip install -e ./antigravity_pro` ทับ Community
- ไม่ใช่ fork แยก — engine เดียวกัน

#### ต้องมีก่อนซื้อ

- [Antigravity Community](https://github.com/borbaibank/antigravity-community) (ฟรี)
- Python 3.11+
- Binance USDT-M Futures

#### ติดตั้ง

1. Clone Community  
2. แตก zip Pro → `antigravity_pro/` ข้าง `server.py`  
3. `pip install -e ./antigravity_pro`  
4. `python server.py` → Tab11–18 ใน dashboard  

#### License

ห้ามแจกต่อ / resell / public fork ของ Pro · 1 buyer / 1 deployment

#### คำเตือน

ไม่รับประกันกำไร · ทดสอบ paper/testnet ก่อน live

---

**English:** Antigravity Pro unlocks Tab11–Tab18 for the free Community bot. Self-hosted Python, `pip install -e ./antigravity_pro`. Commercial license.

---

## Cross-links

| ที่ | URL |
|----|-----|
| Community | https://github.com/borbaibank/antigravity-community |
| Pro | https://borbaibank.gumroad.com/l/antigravity-pro |
| Dev repo (private) | https://github.com/borbaibank/multitrategy |
