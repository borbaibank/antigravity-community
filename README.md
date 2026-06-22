# Antigravity Community Edition

**Antigravity** — บอทเทรด Binance USDT-M Futures แบบ multi-strategy รันบนเครื่องตัวเอง (self-hosted)  
รุ่น Community เปิดให้ใช้ฟรี **10 กลยุทธ์ (Tab1–Tab10)** พร้อม FastAPI dashboard

> ต้องการ Tab11–Tab18 (Volume / Momentum pack)? ดู **[Antigravity Pro](https://tkbanker.gumroad.com/l/binance-multistrategy)** — add-on แยกติดตั้งทับ Community

---

## ฟีเจอร์หลัก

- Hedge mode — รองรับ Long/Short พร้อมกันต่อ symbol
- Paper mode + Live mode (Binance testnet / mainnet)
- Dashboard real-time (WebSocket) — เปิด/ปิด tab, notional, SL/TP mode, scan universe
- 10 กลยุทธ์ บน timeframe 1H และ 4H
- State เก็บใน `paper_state.json` (รันต่อได้หลัง restart)

---

## กลยุทธ์ที่รวมใน Community (Tab1–Tab10)

| Tab | TF | กลยุทธ์ |
|-----|-----|---------|
| Tab1 | 4h | EMA Pullback 110/190 |
| Tab2 | 4h | EMA Cross + ATR |
| Tab3 | 4h | SMC Order Block |
| Tab4 | 4h | Premium/Discount OTE |
| Tab5 | 1h | RSI Divergence |
| Tab6 | 4h | BB/KC Squeeze |
| Tab7 | 4h | CCI |
| Tab8 | 1h | Three Soldiers / Crows |
| Tab9 | 1h | Impulse Continuation |
| Tab10 | 1h | Volume Range Expansion |

---

## Antigravity Pro (ขายแยก)

| Tab | TF | กลยุทธ์ |
|-----|-----|---------|
| Tab11 | 1h | Volume Pressure Proxy |
| Tab12 | 1h | Volume Spike Breakout |
| Tab13–16 | 4h | Clone ของ Tab9–12 บน 4H |
| Tab17 | 1h | Momentum Vol Pressure |
| Tab18 | 1h | Vol ultimate |

ติดตั้ง Pro หลัง Community:

```powershell
pip install -e ./antigravity_pro
```

รายละเอียด: [Antigravity Pro on Gumroad]([https://tkbanker.gumroad.com/l/binance-multistrategy](https://tkbanker.gumroad.com/l/gsdcjhv))

---

## ความต้องการระบบ

- Python **3.11+**
- Windows / Linux / macOS
- บัญชี Binance USDT-M Futures (แนะนำทดสอบ testnet ก่อน)

---

## ติดตั้ง (Quick start)

```powershell
git clone https://github.com/borbaibank/antigravity-community.git
cd antigravity-community

python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

copy .env.example .env
# แก้ .env — ใส่ API key, LIVE_MODE, ORDER_ENV

.\.venv\Scripts\python.exe server.py
```

เปิด dashboard: `http://localhost:8765` (หรือพอร์ตตาม `DASHBOARD_PORT` ใน `.env`)

---

## ตัวแปรสำคัญใน `.env`

| ตัวแปร | ความหมาย |
|--------|----------|
| `LIVE_MODE` | `false` = paper, `true` = ส่งออเดอร์จริง |
| `ORDER_ENV` | `testnet` หรือ `mainnet` |
| `BINANCE_API_KEY` / `SECRET` | API Futures |
| `DASHBOARD_PORT` | พอร์ต dashboard (default 8765) |
| `NOTIONAL_SIZE` | ขนาด notional ต่อไม้ (USD) |

ดูครบใน `.env.example`

---

## โครงสร้างโปรเจกต (ย่อ)

```
server.py          # entry point
config.py          # ค่าคงที่ + env
strategies.py      # signal Tab1–Tab10
bot/               # engine, API, state
static/index.html  # dashboard
paper_state.json   # runtime state (สร้างตอนรัน — อย่า commit)
```

---

## คำเตือน

ซอฟต์แวร์นี้เป็นเครื่องมือเทรด **ไม่ใช่คำแนะนำการลงทุน**  
การเทรด futures มีความเสี่ยงสูง — ทดสอบ paper/testnet ก่อนใช้เงินจริง  
ผู้พัฒนาไม่รับผิดชอบ loss จากการใช้งาน

---

## License

Community Edition — **MIT** (ดู `LICENSE`)

Antigravity Pro (Tab11–Tab18) — commercial license แยกต่างหาก

---

## Support

- Issues: [GitHub Issues](https://github.com/borbaibank/antigravity-community/issues)
- Pro buyers: [Gumroad](https://tkbanker.gumroad.com/l/binance-multistrategy) receipt + `INSTALL.md` ใน zip
