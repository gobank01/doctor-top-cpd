# บันทึกการตรวจ Doctor Top CPD

วันที่ตรวจ: 8 ตุลาคม 2026

## ผลที่ยืนยันแล้ว

- สร้าง public repository: [gobank01/doctor-top-cpd](https://github.com/gobank01/doctor-top-cpd)
- สร้าง Vercel project `doctor-top-cpd` และ private Blob ใหม่แยกจากสตูดิโอเดิมใน region `sin1`
- ตั้ง Gemini API ที่เจ้าของอนุญาต, Blob token ของ store ใหม่ และ session secret ใหม่เป็น environment ฝั่ง server
- Read-only check อ่านข้อมูลโมเดล Gemini สำเร็จ ไม่สร้างเสียงหรือโปรไฟล์แบบเสียเงิน
- Python unit tests **65 รายการผ่าน** ด้วย mock provider; Node recorder tests **19 รายการผ่าน**
- JavaScript syntax ผ่าน และตรวจ DOM IDs **62 รายการไม่ซ้ำ**
- Browser QA ของ cloud app ที่รัน local เชื่อมบริการจริงผ่านที่ขนาด **320 / 390 / 768 / 1440 px × 3 views**: ไม่มี JavaScript errors หรือหน้ารหัส, draft restore ทำงาน, API readiness พร้อม, คลังเสียง/ประวัติว่าง และตรวจภาพหน้าจอ 390/1440 px แล้ว
- private Blob ใหม่นับเริ่มต้น **0 objects**; probe เขียน อ่าน และลบสำเร็จ ไม่มีไฟล์เสียงของเจ้าของถูกย้ายมา
- ตรวจการเปิดเว็บแบบไม่มีรหัส, session แยก pending uploads, same-origin writes, การไม่เปิดเผย Google profile inventory และปฏิเสธ custom voice IDs ที่ไม่อยู่ในคลัง
- ตรวจ source แยกโดยผู้ตรวจอีกคน ไม่มีประเด็นขัดขวางที่รายงาน
- helper `create_session_secret.py` สร้าง secret สุ่ม 32 bytes ลงไฟล์เท่านั้น: ไม่แสดงค่าใน stdout/stderr, Unix mode `0600`, ไม่เขียนทับไฟล์เดิม ทดสอบในโฟลเดอร์ชั่วคราวและลบแล้ว
- `scripts/check_release.py` ผ่าน: ตรวจ **31 public source files** ไม่มี credential/path patterns ที่ตั้งให้ตรวจ; local links ใน README/docs ถูกต้อง

## Production ที่ยืนยันแล้ว

- [x] GitHub `main` เชื่อมกับ Vercel และ deployment สถานะ **Ready** ที่ [doctor-top-cpd.vercel.app](https://doctor-top-cpd.vercel.app)
- [x] เปิดผ่าน HTTPS ได้ HTTP 200 โดยไม่ถามรหัสผ่าน; Gemini key, SDK และ FFmpeg พร้อม; คลังเสียงและประวัติเริ่มว่าง; ไม่เปิดเผย remote profile inventory
- [x] Browser QA บนเว็บ production ผ่าน **320 / 390 / 768 / 1440 px × 3 views**: ไม่ล้นแนวนอน ไม่มี JavaScript errors, คุกกี้ anonymous ทำงาน, ร่างบทกลับมาหลัง reload และตรวจภาพหน้าจอมือถือ/desktop แล้ว

การตรวจเว็บจริงข้างต้นอ่านสถานะและทดสอบหน้าเว็บ ไม่เรียกสร้างเสียงแบบเสียเงิน ไม่มีการอัดหรือย้ายเสียงของเจ้าของเดิม

## ขอบเขตการยืนยัน

เทสต์อัตโนมัติใช้ mock provider, ไฟล์ชั่วคราว และ mock media APIs ไม่สร้างเสียง Gemini ไม่อัดไมค์จริง และไม่ยืนยันคุณภาพ/ความเหมือนของเสียง การอ่านข้อมูลโมเดลได้ยืนยันการเชื่อมคีย์แบบ read-only เท่านั้น ไม่ยืนยันการสร้างโปรไฟล์/สังเคราะห์เสียงครบขั้น

ยังไม่ได้ทดสอบไมค์บนโทรศัพท์จริง การติดตั้งบน Windows/Linux จริง และการสร้างเสียงครบขั้นกับเจ้าของเสียงผ่าน production อย่าอ้างว่าสามส่วนนี้ผ่านจากผล mock tests
