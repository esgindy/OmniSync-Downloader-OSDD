# OmniSync-Downloader (OSDD)

<div align="center">

**Resilient Multi-Source File Streamer & Sync Engine**  
*أداة التحميل المباشر والمزامنة الذكية للملفات والمجلدات الضخمة*

[![Release](https://img.shields.io/badge/Release-v2.0.0-success.svg)](https://github.com/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()
[![Async Engine](https://img.shields.io/badge/Engine-Starlette%20%2B%20HTTPX-green.svg)]()

[English](#-english) • [العربية](#-باللغة-العربية)

</div>

---

## 🇬🇧 English

### 🌟 Overview

**OmniSync-Downloader (OSDD)** is a high-performance, open-source tool and web dashboard designed to eliminate the common headaches of downloading and synchronizing large directories from cloud and local/network storage.

- In **v1.0**, OSDD solved Microsoft SharePoint and OneDrive folder downloads by pulling files **directly file-by-file**, bypassing server-side zip compression that leads to corrupted archives, download timeouts, and the restrictive 250 GB / 10,000 file caps.
- In **v2.0**, OSDD introduces a native **Drive-to-Drive / Disk-to-Disk Sync & Deep Audit Engine** with chunked streaming SHA-256 verification (< 5MB RAM even for 100GB+ files), live dashboard indicators (speed, downloaded vs. remaining volume, remaining files), 1-click silent background execution, and automatic process termination when the browser closes.

---

### ✨ Features (v2.0)

#### 💽 Drive-to-Drive / Disk-to-Disk Sync & Deep Audit (New in v2.0)
- **Directory Tree Comparison:** Compare folders across different drives, external USB HDDs, or NAS network shares.
- **Delta Sync (Copy-based, Non-destructive):** Detects and copies **only new and modified files**; original source files are untouched.
- **Streaming SHA-256 Deep Audit:** 1MB chunked cryptographic hash verification with **< 5MB RAM consumption** even on massive 100GB+ files.
- **Safe Atomic Copying:** Uses `.sync_part` and atomic rename with original timestamp preservation (`shutil.copystat`).
- **Live Transfer Telemetry:** Real-time speedometer (MB/s), copied vs. remaining bytes, remaining files, current active file, and full pause/resume/cancel controls.

#### 📊 Live Download Dashboard KPIs (New in v2.0)
- **⚡ Live Transfer Speedometer:** Instantaneous download and sync transfer speed in KB/s and MB/s.
- **💾 Explicit Remaining Bytes:** Shows downloaded volume alongside remaining volume clearly (e.g., `14.2 GB / 50.0 GB (Remaining: 35.8 GB)`).
- **📄 Remaining Files Counter:** Shows completed files vs. remaining files (e.g., `120 / 500 (Remaining: 380)`).
- **⏱️ Dynamic ETA & Progress:** Accurate estimated completion time based on live transfer speed.

#### 🚀 1-Click Silent Background Launch & Smart Auto-Close (New in v2.0)
- **`start.bat` & `start.vbs`:** 1-Click launch directly into the background without popping any black CMD console window.
- **Automatic Browser Launch:** Directly opens `http://127.0.0.1:8765` in your default browser.
- **Intelligent Auto-Shutdown:** Automatically stops the Python background server and cleans RAM when the browser window/tab is closed (Heartbeat + `pagehide` beacon).
- **`stop.bat` & UI Shutdown Button:** Manual 1-click stop script and an in-app shutdown button in the topbar.

#### ☁️ SharePoint & OneDrive Direct File-by-File Streaming (v1.0 Core)
- **Direct File-by-File Streaming:** Downloads entire folder trees file-by-file with exact folder hierarchy on disk.
- **True HTTP Range Resumption:** Interrupted connections resume byte-for-byte from partial `.part` files without restarting from zero.
- **Smart Browser Authentication:** Automated login via Edge/Chrome to capture FedAuth & session cookies, with fallback cURL / cookie import.
- **Integrity Audit & Verification:** Post-download byte-level comparison, missing file detection, and CSV / JSON export.
- **Windows Long Path & Unicode Support:** Native handling of paths exceeding 260 characters (`\\?\` prefix) and robust UTF-8 Arabic character sanitization.

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
- **Windows (1-Click Silent Start - Recommended):** Double-click **`start.bat`** (or `start.vbs`).
  - Launches silently in the background (no black CMD window).
  - Automatically opens the web interface in your default browser.
  - Automatically terminates when you close the browser window, or via **`stop.bat`**.
- **Windows (Console Mode):** Double-click **`run.bat`** if you wish to see live server logs in CMD.
- Open your browser at: **`http://127.0.0.1:8765`**

---

## 🇸🇦 باللغة العربية

### 🌟 نظرة عامة

**OmniSync-Downloader (OSDD)** هي أداة برمجية مفتوحة المصدر عالية الأداء تم بناؤها للتغلب على مشاكل تحميل ومزامنة المجلدات والمكتبات الضخمة من السحابة ووحدات التخزين المحلية والشبكية.

- في **الإصدار v1.0**، ركز البرنامج على حل مشاكل تحميل مجلدات **Microsoft SharePoint** و **OneDrive** عبر سحب الملفات مباشرة **ملفاً بملف** دون تجميعها في ملف مضغوط (Zip)، مما يمنع تلف الملفات ويتجاوز حدود شيربوينت الصارمة (250GB أو 10,000 ملف).
- في **الإصدار v2.0**، يقدم البرنامج ميزة **مزامنة وتدقيق الملفات بين هاردين أو جهازين (Drive-to-Drive Sync & Deep Audit)** بفحص الهاش المتدفق (SHA-256) للملفات العملاقة (+100GB) باستهلاك رام أقل من 5 ميجابايت، مع مؤشرات حية في الداش بورد لحجم وعدد الملفات المتبقية والسرعة اللحظية، وتشغيل صامت بالكامل وإيقاف تلقائي عند إغلاق المتصفح.

---

### ✨ أبرز مميزات الإصدار v2.0

#### 💽 مزامنة وتدقيق هارد لهارد (جديد في v2.0)
- **مقارنة شجرة المجلدات:** فحص ومقارنة محتويات مجلدين على هاردين مختلفين أو عبر الشبكة (NAS / SMB).
- **مزامنة ذكية بالنسخ (Delta Sync Copy):** نقل ونسخ **الملفات الجديدة والمعدلة فقط** مع الحفاظ على ملفات المصدر الأصلية بنسبة 100% دون مساس بها.
- **فحص التطابق العميق (Streaming SHA-256):** فحص رقمي متدفق بقطع 1 ميجابايت يستهلك **أقل من 5 ميجابايت رام** حتى مع الملفات الضخمة جداً (+100GB).
- **أمان النسخ الفعلي:** نسخ مؤقت بصيغة `.sync_part` مع استبدال ذري آمن ونقل تواريخ التعديل الأصلية (`copystat`).

#### 📊 مؤشرات تفاعلية حية في الداش بورد (جديد في v2.0)
- **⚡ عداد سرعة التحميل اللحظية:** قياس مستمر لمعدل النقل المباشر بوحدة KB/s و MB/s.
- **💾 إحصائيات الحجم المحمل والمتبقي:** عرض الحجم المنجز بجانب **الحجم المتبقي الصافي بشكل صريح** (مثال: `المتبقي: 35.8 GB`).
- **📄 إحصائيات عدد الملفات المكتملة والمتبقية:** عرض عدد الملفات المنجزة والمتبقية بدقة.
- **⏱️ الوقت المتبقي المتوقع (ETA):** احتساب الزمن المتبقي بناءً على سرعة النقل الحية.

#### 🚀 التشغيل الصامت والإيقاف التلقائي الذكي (جديد في v2.0)
- **`start.bat` و `start.vbs`:** تشغيل صامت بنقرة واحدة في الخلفية بدون ظهور نافذة موجه الأوامر السوداء.
- **فتح المتصفح تلقائياً:** يتوجه المتصفح الافتراضي فوراً لواجهة البرنامج على المنفذ `8765`.
- **إيقاف تلقائي عند إغلاق المتصفح:** محرك نبضات (Heartbeat) يكتشف إغلاق نافذة المتصفح ويوقف خادم البايثون تلقائياً ويفرغ الذاكرة.
- **ملف `stop.bat` وزر إغلاق في الواجهة:** لإيقاف كافة العمليات في أي وقت بنقرة واحدة.

#### ☁️ مميزات الإصدار الأساسي v1.0 (شيربوينت وون درايف)
- **تحميل مباشر بدون ضغط (No Zip Limits):** سحب المجلدات والملفات بهيكليتها الأصلية بدقة، وتفادي تلف ملفات الـ Zip الناتجة عن خوادم مايكروسوفت.
- **استئناف حقيقي للتنزيل (HTTP Range Resume):** استكمال الملفات المنقطعة من آخر بايت تم تحميله عبر إدارة ملفات `.part`.
- **تسجيل دخول ذكي وسلس:** التقاط تلقائي لكوكيز المصادقة (FedAuth / rtFa) من خلال متصفح تفاعلي (Edge أو Chrome).
- **فحص سلامة واكتمال التحميل (Verification Audit):** تدقيق شامل بعد انتهاء التحميل وتصدير تقارير CSV و JSON.
- **دعم المسارات الطويلة واللغة العربية:** دعم مسارات ويندوز (`\\?\`) والتعامل السليم مع الأسماء العربية.

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
   - **التشغيل الصامت بنقرة واحدة (مستحسن):** اضغط مرتين على ملف **`start.bat`** (أو `start.vbs`).
     - يعمل البرنامج في الخلفية بدون شاشة سوداء.
     - يفتح المتصفح الافتراضي تلقائياً على واجهة الأداة.
     - يتوقف تلقائياً عند إغلاق نافذة المتصفح، أو بالضغط على ملف **`stop.bat`**.
   - **وضع السجلات المباشرة:** اضغط مرتين على ملف **`run.bat`** لمتابعة تفاصيل السيرفر في شاشة موجه الأوامر.
5. توجه في المتصفح إلى: **`http://127.0.0.1:8765`**

---

## 🔒 الخصوصية والأمان / Privacy & Security

- الأداة تعمل محلياً 100%، ولا يتم إرسال أي بيانات أو جلسات أو كوكيز إلى أي خادم خارجي.

---

## 📄 الترخيص / License

هذا المشروع مرخص تحت رخصة **MIT License**.
