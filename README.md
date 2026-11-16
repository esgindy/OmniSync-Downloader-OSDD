# OmniSync-Downloader (OSDD)

<div align="center">

**Resilient Multi-Source File Streamer & Sync Engine**  
*أداة التحميل المباشر والمزامنة الذكية للملفات والمجلدات الضخمة*

[![Release](https://img.shields.io/badge/Release-v1.0.0-blue.svg)](https://github.com/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()
[![Async Engine](https://img.shields.io/badge/Engine-Starlette%20%2B%20HTTPX-green.svg)]()

[English](#-english) • [العربية](#-باللغة-العربية) • [Roadmap](#-roadmap--خريطة-التطوير)

</div>

---

## 🇬🇧 English

### 🌟 Overview

**OmniSync-Downloader (OSDD)** is a high-performance, open-source tool and web dashboard designed to eliminate the common headaches of downloading and synchronizing large directories from cloud and remote storage.

In **v1.0**, OSDD tackles Microsoft SharePoint and OneDrive folder downloads by pulling files **directly file-by-file**, bypassing server-side zip compression that frequently leads to corrupted archives, download timeouts, and the restrictive 250 GB / 10,000 file caps.

---

### ✨ Features (v1.0)

- **🚀 Direct File-by-File Streaming:** Downloads entire folder trees file-by-file with exact folder hierarchy on disk.
- **🔄 True HTTP Range Resumption:** Interrupted connections resume byte-for-byte from partial `.part` files without restarting from zero.
- **🔑 Smart Browser Authentication:** Automated login via Edge/Chrome to capture FedAuth & session cookies, with fallback cURL / cookie import.
- **⚡ Synchronize & Delta Engine:** Compares local folders against cloud storage to detect new, modified, or missing files.
- **🛡️ Integrity Audit & Verification:** Post-download byte-level comparison, missing file detection, and CSV / JSON export.
- **📂 Windows Long Path & Unicode Support:** Native handling of paths exceeding 260 characters (`\\?\` prefix) and robust UTF-8 Arabic character sanitization.
- **🖥️ Real-time Modern Dashboard:** Responsive, bilingual (RTL/LTR) web UI with live SSE progress bars, speed metrics, and active worker telemetry.

---

### 🗺️ Roadmap / خريطة التطوير

- **v1.0 (Current):** SharePoint & OneDrive direct folder streaming, auto-resuming, delta sync & integrity verification.
- **v2.0 (Upcoming):** Local Drive-to-Drive / Disk-to-Disk & Network Storage (NAS/SMB) synchronization with chunked streaming SHA-256 integrity verification.
- **v3.0 (Upcoming):** Google Drive integration for direct folder streaming and multi-cloud sync.

---

### 🚀 Quick Start

#### Prerequisites
- **Python 3.10+** installed and added to `PATH`.

#### 1. Clone & Install
```bash
git clone https://github.com/esgindy/OmniSync-Downloader-OSDD.git
cd OmniSync-Downloader-OSDD

# Install dependencies
pip install -r requirements.txt

# (Optional) Install Playwright browser drivers for automatic login
playwright install chromium
```

#### 2. Run the Application
- **On Windows:** Double-click `run.bat` or run:
  ```cmd
  python app.py
  ```
- Open your browser at: **`http://localhost:8000`**

---

## 🇸🇦 باللغة العربية

### 🌟 نظرة عامة

**OmniSync-Downloader (OSDD)** هي أداة برمجية مفتوحة المصدر عالية الأداء تم بناؤها للتغلب على مشاكل تحميل ومزامنة المجلدات والمكتبات الضخمة من السحابة ووحدات التخزين.

في **الإصدار v1.0**، يركز البرنامج على حل مشاكل تحميل مجلدات **Microsoft SharePoint** و **OneDrive** عبر سحب الملفات مباشرة **ملفاً بملف** دون تجميعها في ملف مضغوط (Zip)، مما يمنع تلف الملفات ويتجاوز حدود شيربوينت الصارمة (250GB أو 10,000 ملف).

---

### ✨ أبرز مميزات الإصدار v1.0

- **تحميل مباشر بدون ضغط (No Zip Limits):** سحب المجلدات والملفات بهيكليتها الأصلية بدقة، وتفادي تلف ملفات الـ Zip الناتجة عن خوادم مايكروسوفت.
- **استئناف حقيقي للتنزيل (HTTP Range Resume):** استكمال الملفات المنقطعة من آخر بايت تم تحميله دون الحاجة لإعادة التنزيل من الصفر عبر إدارة ملفات `.part`.
- **تسجيل دخول ذكي وسلس:** التقاط تلقائي لكوكيز المصادقة (FedAuth / rtFa) من خلال متصفح تفاعلي (Edge أو Chrome) مع دعم الاستيراد اليدوي لنصوص الـ cURL.
- **محرك مزامنة ومطابقة ذكي (Sync & Diff):** فحص المجلد المحلي ومقارنته بالسحابة، وتحميل الملفات الجديدة أو المعدلة فقط لتوفير استهلاك الإنترنت.
- **فحص سلامة واكتمال التحميل (Verification Audit):** تدقيق شامل بعد انتهاء التحميل لمطابقة الأحجام واكتشاف أي ملفات ناقصة وتصدير تقارير بصيغة CSV و JSON.
- **دعم المسارات الطويلة واللغة العربية:** دعم مسارات ويندوز التي تتجاوز 260 حرفاً (`\\?\`) والتعامل السليم مع الأسماء العربية والرموز الخاصة بدون أخطاء تشفير.
- **لوحة تحكم عصرية تدعم العربية (RTL):** واجهة ويب محلية فائقة السرعة مع تحديثات لحظية (SSE) لنسب الإنجاز وسرعة النقل وحالة كل ملف.

---

### 🗺️ خريطة الإصدارات القادمة

- **الإصدار v1.0 (الحالي):** تحميل ومزامنة مجلدات شيربوينت وون درايف بالكامل بدون ضغط مع الاستئناف الذكي والفحص.
- **الإصدار v2.0 (التالي):** مزامنة كاملة بين هاردين أو جهازين (Disk-to-Disk / Drive-to-Drive / NAS) مع فحص تطابق عميق (Streaming SHA-256) للملفات الضخمة.
- **الإصدار v3.0:** إضافة دعم Google Drive للتحميل المباشر والمزامنة المتعددة.

---

### 💻 خطوات التشغيل السريع

1. تأكد من تثبيت **Python 3.10+**.
2. قم باستنساخ المستودع:
   ```bash
   git clone https://github.com/esgindy/OmniSync-Downloader-OSDD.git
   cd OmniSync-Downloader-OSDD
   ```
3. تثبيت المكتبات المطلوبة:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
4. تشغيل البرنامج:
   - على نظام ويندوز: اضغط مرتين على ملف **`run.bat`**
   - أو عبر سطر الأوامر:
     ```bash
     python app.py
     ```
5. توجه في المتصفح إلى: **`http://localhost:8000`**

---

## 🔒 الخصوصية والأمان / Privacy & Security

- الأداة تعمل محلياً 100%، ولا يتم إرسال أي بيانات أو جلسات أو كوكيز إلى أي خادم خارجي.

---

## 📄 الترخيص / License

هذا المشروع مرخص تحت رخصة **MIT License**.
