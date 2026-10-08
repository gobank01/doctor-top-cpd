# ติดตั้ง Doctor Top CPD บน Vercel

คู่มือนี้สำหรับผู้ดูแลหรือผู้ที่นำ repo ไปติดตั้งใหม่ ผู้ใช้เว็บที่ติดตั้งแล้วเปิด URL ได้ทันที ไม่ต้องกรอก API key หรือรหัสผ่าน

Doctor Top CPD ใช้ Gemini API ที่เจ้าของอนุญาตให้เชื่อม แต่แยก Vercel project, private Blob และ session secret ใหม่ ไม่มีเสียงหรือประวัติจากสตูดิโอเดิม ผู้ที่นำ repo ไปติดตั้งเองต้องใช้บัญชีและคีย์ที่ตนได้รับอนุญาต ไม่สามารถเรียกคีย์ของ deployment นี้จาก repo ได้

## 1. เตรียม repository

Fork [gobank01/doctor-top-cpd](https://github.com/gobank01/doctor-top-cpd) หรือสร้าง repo ใหม่จาก source อย่าคัดลอก `.env*`, `.vercel`, `.venv`, `samples/`, `out/`, `voices.json` หรือข้อมูลจริงของใคร

ก่อน push รัน `python3 scripts/check_release.py` และตรวจ `git status` / `git diff --cached` ด้วยตนเอง `.env.example` ต้องมีเฉพาะชื่อตัวแปรและค่าว่าง

## 2. สร้าง Vercel project และ private Blob ใหม่

1. เข้าบัญชี [Vercel](https://vercel.com/) → Add New Project → Import repo ของตนเอง
2. Root directory ต้องมี `app.py` และ `requirements.txt` ใช้ framework **FastAPI** ตาม `vercel.json` และ Python 3.13 ตาม `.python-version` ไม่มี frontend build
3. ไป Storage → สร้าง Blob store **ใหม่แบบ Private** → เชื่อมกับ project นี้
4. ตรวจว่า `BLOB_READ_WRITE_TOKEN` เป็นของ store ใหม่นี้ ห้ามใช้ token ของสตูดิโอเดิม

อ้างอิงขั้นตอนปัจจุบันที่ [FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi), [Python runtime](https://vercel.com/docs/functions/runtimes/python) และ [Vercel Blob](https://vercel.com/docs/vercel-blob)

Private Blob ปิดการอ่านไฟล์ตรงจาก public Blob URL แต่ Doctor Top CPD ให้ผู้มี URL สตูดิโอเข้าถึงไฟล์ที่บันทึกแล้วผ่านแอปได้ เพราะผู้ดูแลเลือก **ไม่มีรหัสผ่าน** ไม่มีบัญชีหรือสิทธิ์แยกบุคคล

## 3. สร้าง session secret

macOS / Linux:

```bash
python3 scripts/create_session_secret.py
```

Windows:

```powershell
py -3 scripts/create_session_secret.py
```

สคริปต์สร้าง `.env.cloud-session` ที่มี `SESSION_SECRET` แบบสุ่ม ไม่ถามรหัสผ่าน ไม่พิมพ์ secret ใน Terminal และไม่เขียนทับไฟล์เดิม เปิดไฟล์นี้ในเครื่องแล้วคัดลอกค่าลง Vercel อย่างเป็นส่วนตัว

`SESSION_SECRET` ใช้ลงนามคุกกี้ชั่วคราวเพื่อผูกคลิปที่กำลังอัปโหลดกับเบราว์เซอร์ ไม่ใช่ระบบล็อกอินหรือการจำกัดผู้เข้าใช้เว็บ ห้าม commit หรือส่งไฟล์นี้ให้ผู้ใช้เว็บไซต์

## 4. ตั้ง environment ทั้ง 3 ค่า

| ตัวแปร | ค่าและหน้าที่ |
|---|---|
| `GEMINI_API_KEY` | คีย์ Google AI Studio ที่เจ้าของอนุญาต พร้อมสิทธิ์โมเดล โควตา และ Billing ที่เหมาะสม |
| `BLOB_READ_WRITE_TOKEN` | token ของ **private Blob แยกของ installation นี้** |
| `SESSION_SECRET` | ค่าสุ่มใหม่จาก `scripts/create_session_secret.py` |

ตั้งใน Vercel Environment Variables สำหรับ **Production** ไม่มีตัวแปรรหัสผ่าน ไม่ใส่คีย์ใน HTML, JavaScript หรือค่าที่ส่งให้ client หากใช้ Preview ให้กำหนด API/Blob ของ Preview อย่างชัดเจนเพื่อไม่ให้ข้อมูลทดสอบปน production

**เพิ่มหรือเปลี่ยน env แล้วต้อง Redeploy** จึงจะมีผลกับ deployment ใหม่

## 5. Deploy และตรวจเว็บจริง

กด Deploy / Redeploy แล้วรอ **Ready** เปิด URL HTTPS ที่ Vercel ส่งกลับ ตรวจ URL จริงก่อนส่งให้เพื่อน ชื่อ `doctor-top-cpd.vercel.app` เป็นชื่อเป้าหมายของ deployment นี้ ไม่ควรเดา URL ของสำเนาที่ติดตั้งใหม่

ตรวจแบบไม่เสียค่า API:

1. เปิด `/` ได้ทันทีโดยไม่ถามรหัส; `/login` เก่าให้กลับมาหน้าสตูดิโอ
2. มีชื่อ Doctor Top CPD และปุ่ม **เพิ่มเสียงใหม่ / สร้างเสียงพูด**
3. สถานะ Gemini และที่เก็บข้อมูลพร้อม คลังเสียงและประวัติว่างใน installation ใหม่
4. เปิด Safari/Chrome บนมือถือผ่าน HTTPS ตรวจการนำทาง การแสดงผล และการขอสิทธิ์ไมค์
5. ตรวจว่า API ไม่แสดงคีย์หรือรายชื่อโปรไฟล์เสียงเดิมจาก Google project

การตรวจครบขั้นด้วยเสียงจริงต้องให้เจ้าของเสียงอัดคลิปและประโยคยินยอม แล้วสร้างเสียงจากบทสั้น ฟัง ดาวน์โหลด และรีเฟรชเพื่อตรวจประวัติ ขั้นนี้เรียก Gemini และมีโอกาสเสียค่าใช้จ่าย **อย่าอ้างว่าการผ่าน mock tests ยืนยันผลเสียงจริงหรือไมค์บนโทรศัพท์แล้ว**

## ใช้ Vercel CLI

ติดตั้ง Node.js แล้วรันจากโฟลเดอร์ repo นี้:

```bash
npx vercel login
npx vercel link
npx vercel env add GEMINI_API_KEY production
npx vercel env add SESSION_SECRET production
npx vercel --prod
```

เลือก scope/project ให้ตรง กรอกค่าลับผ่าน prompt โดยไม่แชร์ค่าลงแชต `BLOB_READ_WRITE_TOKEN` มาจากการเชื่อม Storage หรือเพิ่มใน Environment Variables อย่าเชื่อม CLI จากโฟลเดอร์แม่ของหลายโปรเจกต์

## ดูแลหลังติดตั้ง

- `vercel.json` ตั้ง `maxDuration: 300` วินาที งานบทพูดยาวอาจ timeout ขีดจำกัดจริงขึ้นกับแผนและ [Function duration](https://vercel.com/docs/functions/configuring-functions/duration)
- การไม่มีรหัสทำให้ผู้มี URL ใช้โควตาและเข้าถึงคลังร่วมได้ ตรวจค่าใช้จ่ายของ Gemini, Functions และ Blob ตามการใช้งาน ไม่มีโควตาแยกต่อคน
- เปลี่ยน session secret โดยย้าย `.env.cloud-session` เดิมไปที่เก็บส่วนตัว รัน helper ใหม่ อัปเดต env แล้ว redeploy คลิปที่ยังอัปโหลดค้างใน session เดิมอาจต้องอัดใหม่
- สำรองไฟล์/metadata ของ Blob ลงพื้นที่ส่วนตัว การลบ deployment ไม่ได้ลบ Blob และการลบ Blob ไม่ได้ลบโปรไฟล์ที่ Google
- หากเปลี่ยน Gemini project โปรไฟล์เดิมอาจใช้ต่อไม่ได้ ต้องใช้ project เดิมหรือสร้างเสียงใหม่พร้อมความยินยอม
