# OmniSync-Downloader (OSDD) - وثيقة تسليم المشروع والملخص الشامل (Project Handover Summary)

> **تاريخ التحديث:** 17 نوفمبر 2026  
> **مسار المشروع المحلي:** `D:\Gindy Work\Anti Gi\sharepoint-downloader`  
> **مستودع GitHub:** `https://github.com/esgindy/OmniSync-Downloader-OSDD`  
> **البيئة المستهدفة:** Windows 10 / 11, Python 3.10+, Starlette, Uvicorn, Playwright, HTTPX, yt-dlp  

---

## 1. فكرة المشروع والهدف الأساسي (Core Mission)
برنامج **OmniSync-Downloader (OSDD)** هو منصة برمجية محلية متقدمة بواجهة ويب عصرية مدمجة، صُممت لحل أكبر مشاكل نقل وتنزيل الملفات والمجلدات السحابية والبيانات الضخمة:
1. **تجاوز قيود الضغط السحابي (Anti-Zip Streaming):** خوادم SharePoint و OneDrive و Google Drive تفرض ضغط المجلدات الكبيرة في ملفات `zip`، مما يسبب انقطاع التحميل، وتلف الأرشيف، والتوقف عند حد 250GB أو 10,000 ملف في شيربوينت، أو حد 2GB في جوجل درايف. يقوم OSDD بتنزيل الملفات مباشرة **ملفاً تلو الآخر** محافظاً على الهيكل الشجري للمجلدات مع دعم استئناف التحميل المقطوع (HTTP Range Resumption عبر ملفات `.part`).
2. **محرك مزامنة وتدقيق الأقراص (Drive-to-Drive Delta Sync & Deep Audit):** مزامنة ذكية بين هاردين أو مسارين محليين/شبكيين، بنسخ الملفات الجديدة والمعدلة فقط، مع فحص تشفيري متدفق عالي الدقة بـ SHA-256 يستهلك **أقل من 5 ميجابايت رام** حتى مع ملفات تفوق 100GB.
3. **محرك تحميل الوسائط الشامل (Universal Media Streamer):** تنزيل الفيديوهات وقوائم التشغيل والصوتيات من YouTube، TikTok، Reels، Facebook، وغيرها مع خيارات الجودة أو استخراج الصوت المباشر إلى MP3.
4. **لوحة مؤشرات علوية حية (Live Dashboard Telemetry):** عداد سرعة لحظي (MB/s)، حجم منجز مقابل الحجم المتبقي بدقة، عدد الملفات المتبقية، ووقت انتهاء تقديري (ETA).
5. **محرك لغات مدمج (8 لغات عالمية):** تبديل فوري بين 8 لغات عالمية مع دعم كامل للاتجاهين (RTL للعربية / LTR للغات الأخرى).
6. **تشغيل صامت وتثبيت تلقائي بنقرة واحدة:** سكربت `install.bat` لتثبيت بايثون والمكتبات بدون أي عناء، وسكربت `start.bat` / `start.vbs` للتشغيل في الخلفية بدون نوافذ سوداء، مع إغلاق المخدم تلقائياً عند إغلاق المتصفح.

---

## 2. سجل التطور والإصدارات (Version Evolution)
* **v1.0 - الأساسي:** تحميل SharePoint و OneDrive ملفاً بملف مع التقاط الكوكيز التلقائي عبر Playwright، ودعم مسارات ويندوز الطويلة (`\\?\`) واللغة العربية، وتقارير الفحص والتدقيق (CSV / JSON).
* **v2.0 - محرك المزامنة ومؤشرات الداش بورد:** إضافة Disk-to-Disk Delta Sync مع تدقيق SHA-256، وتشغيل صامت بالكامل في الخلفية مع ميزة الإنهاء التلقائي عبر الـ Heartbeat عند إغلاق المتصفح.
* **v3.0 - دعم Google Drive وجسر السحابات والتعريب:** إضافة تحميل مجلدات Google Drive المشتركة ملفاً بملف وتجاوز تحذير الفيروسات للملفات الضخمة، جسر النقل المباشر من سحابة إلى سحابة (Cloud-to-Cloud Bridge)، تثبيت لوحة المتابعة الحية في أعلى الواجهة، وإضافة محرك اللغات الثماني.
* **v4.0 - محرك الوسائط والمثبت التلقائي:** تكامل مكتبة `yt-dlp` لتنزيل الفيديوهات والـ Playlists والصوتيات، وتطوير `install.bat` الذكي الذي يكتشف عدم وجود بايثون ويقوم بتحميله وتثبيته في الخلفية صامتاً وضبط PATH.

---

## 3. المشاكل التي تم تشخيصها وحلها اليوم (Today's Bug Fixes)

### 🐛 مشكلة الخطأ الأحمر: `Cannot set properties of null (setting 'textContent')`
* **سبب المشكلة:** في واجهة الويب (`static/index.html`)، عند الضغط على زر **"بدء تحميل الجديد فقط" (`startSyncDownloadBtn`)** أو تلقي تحديثات التقدم عبر الـ SSE، يتم استدعاء دالة `handleSnapshot(snap)`. داخل هذه الدالة في السطر 3099، كان الكود يحاول تحديث عنصر بالمعرف `dashDestDir`:
  ```javascript
  $("dashDestDir").textContent = snap.dest_dir || "-";
  ```
  ولكن العنصر `dashDestDir` لم يكن موجوداً في شجرة الـ DOM للداش بورد العلوية، مما أدى لرمي استثناء `TypeError: Cannot set properties of null`، فتلتقطه دالة الـ `catch` وتُظهر رسالة الخطأ في الإشعار الأحمر وتمنع اكتمال الانتقال التلقائي لمرحلة التحميل.
* **الحل المطبق:** تمت إضافة حماية شرطية (Guard Check) تضمن عدم لمس أي عنصر غير موجود:
  ```javascript
  if ($("dashSpeed")) $("dashSpeed").textContent = snap.status === "running" ? formatSpeed(snap.overall_speed_bps) : "0 KB/s";
  if ($("dashEta")) $("dashEta").textContent = snap.status === "running" ? formatEta(snap.eta_seconds) : "--";
  if ($("dashDestDir")) $("dashDestDir").textContent = snap.dest_dir || "-";
  ```
  وبذلك تم القضاء على الخطأ تماماً وأصبح بدء المزامنة وتدفق البيانات يعمل بسلاسة.

---

## 4. تحسينات صفحة المستودع وملف الـ README
* تم تحسين وتنسيق ملف `README.md` لإبراز:
  - شارات المشروع والإصدار v4.0.0.
  - إبراز اللغات الثماني المدعومة بأعلامها (العربية، الإنجليزية، الإسبانية، الفرنسية، الألمانية، الصينية، الروسية، البرتغالية).
  - جدول المقارنة الفنية (Feature Matrix).
  - مخطط المعمارية التفاعلي (Mermaid Architecture Workflow).
  - خطوات التشغيل السريع المزدوجة (1-Click Auto Install & Manual).
* تم تجهيز بيانات الـ **About** والـ **Description** وقائمة الـ **Topics** لربطها بصفحة المستودع على GitHub لكي يظهر الديسكربشن في الكارت المثبت (Pinned Card) ويسهل البحث عن الأداة.

---

## 5. المعمارية وخريطة ملفات المشروع (File Map)
```
sharepoint-downloader/
├── app.py                   # خادم التطبيق الرئيسي (Starlette / Uvicorn / SSE Endpoints)
├── sp_client.py             # عميل ومحرك فحص واستكشاف شيربوينت وون درايف
├── sp_auth.py               # نظام المصادقة وإدارة الجلسات والكوكيز عبر Playwright
├── downloader.py            # محرك التحميل المتوازي مع دعم استئناف HTTP Range
├── sync_engine.py           # محرك المزامنة الرياضية التناضحية وفحص SHA-256
├── verifier.py              # محرك التحقق والتدقيق بعد التحميل وتوليد التقارير
├── gdrive_client.py         # محرك تحميل وتدفق جوجل درايف
├── media_downloader.py      # محرك تحميل الميديا واليوتيوب (yt-dlp)
├── utils.py                 # دوال مساعدة لحساب المسارات والأحجام والتشفير
├── static/
│   ├── index.html           # واجهة المستخدم الرئيسية (Dashboard + 8 Languages + Tabs)
│   └── favicon.svg          # أيقونة البرنامج
├── requirements.txt         # متطلبات بايثون الأساسية
├── install.bat              # مثبت المتطلبات التلقائي الصامت بنقرة واحدة
├── start.bat                # تشغيل البرنامج في الخلفية
├── start.vbs                # تشغيل صامت 100% بدون شاشة سوداء
├── stop.bat                 # إيقاف خادم التطبيق فوراً
├── run.bat                  # تشغيل كونسول تفاعلي لرؤية السجلات المباشرة
├── README.md                # وثيقة المستودع الرسمية
└── PROJECT_HANDOVER_SUMMARY.md # هذا الملف (ملخص التسليم لكلود)
```

---

## 6. توصيات وتعليمات مهمة للمطور اللاحق (For Claude)
1. **قاعدة الرفع لـ GitHub (`git push`):** لم يتم تشغيل أي أمر `git push` بناءً على تعليمات المستخدم الصارمة. لا تقم بالرفع إلا إذا طلب المستخدم ذلك صراحة.
2. **الـ Sandbox على جهاز الويندوز:** تشغيل أوامر الطرفية على هذا الجهاز يتطلب دائماً تخطي العزل (`BypassSandbox: true` في حال استدعاء `run_command`).
3. **الملفات التكميلية المقترحة:** يمكن مساعدة المستخدم في إضافة ملف `LICENSE` وملفات `.github/ISSUE_TEMPLATE` على GitHub، أو تحديث الـ Repository Description عبر GitHub API / CLI.
4. **تأكيد خلو JavaScript من أي أخطاء:** تم فحص كافة معرفات العناصر (179 معرّف) والتأكد من عدم وجود أي معرّف مفقود يسبب استثناءات runtime أخرى.

---

## 7. الإصدار v6.0 — فرع `v6.0` — آخر تحديث 2026-11-17 (مكتمل، بدون commit بعد)
**المتابعة:** `dev_dashboard/index.html` (تتحدث كل 3 ثوانٍ من `status.js`، بدون توكينز). التحديث: `python dev_dashboard/dev_status.py set <ID> <todo|doing|done|blocked> --agent NAME --note "..."`.

**الأمان/الاستقرار:** `LocalOnlyGuard` في `app.py` (رفض Host/Origin غير محلي و`Sec-Fetch-Site: cross-site`)، إيقاف آمن عبر `_request_exit()` بدل `os._exit`، مهلة heartbeat 25 ثانية، `/api/health` يعيد `version`.
**الأداء:** `downloader.py`/`sp_client.py`/`gdrive_client.py`: HTTP/2 (مع fallback)، حدود اتصالات أعلى، backoff أسي + jitter + `Retry-After`، تحميل متوازٍ بأجزاء Range للملفات >64MB (يكتب `.part.pdl` ثم يُحوَّل لـ`.part` عند الإيقاف)، حد سرعة `speed_limit_kbps`.
**المزامنة:** مقارنة حجم+وقت (±2ث) أولاً ثم SHA-256 للمشكوك فيه فقط (تغيير سلوك: حجم متطابق ووقت مختلف صار يُفحص بالـ hash)، `plan_sync_dry_run`، `find_duplicates`.
**الميزات (وحدات جديدة):** `filters.py`، `history_db.py` (SQLite)، `notifier.py` (toast/Telegram)، `job_queue.py`، `scheduler.py`. مسارات: `/api/filters/test` `/api/history` `/api/settings/notify` `/api/notify/test` `/api/queue*` `/api/schedule` `/api/disk/dryrun` `/api/disk/duplicates`. الملفات `osdd_history.db` و`osdd_settings.json` و`osdd_schedules.json` في `.gitignore`.
**الواجهة:** 5 ثيمات + auto، تبويبات، sparkline، تبويب سجل/أتمتة، فلاتر، حد سرعة، dry-run، تكرارات، إشعارات (token للكتابة فقط).
**الجودة:** `.github/workflows/ci.yml`، `ruff.toml`، `requirements.lock.txt`.

**التحقق:** `python -m unittest discover -s tests` = 55 اختباراً ناجحاً (~22ث). فُحصت الصفحة على الخادم الفعلي: لا أخطاء console، التبويبات والثيم و`/api/history` تعمل. لم تُجرَّب تحميلات حقيقية من SharePoint/Drive/yt-dlp في v6، ولا تحميل متوازٍ مقابل خادم حقيقي (اختُبر على خادم محلي فقط).
**ملاحظات:** مهمة disk-sync قصيرة جداً قد لا تُسجَّل في السجل (polling كل ثانية). ملف `AGENTS.md` القديم يقول "لا commits معلقة" وهذا لم يعد صحيحاً: كل عمل v6 غير ملتزَم به. لم يُنفَّذ `git push`.

**نتائج فحص الواجهة الفعلي (ui-verify، 2026-11-17، متصفح مدمج على الخادم الحي):**
- نجح: لا أخطاء console؛ `/api/health` بـ Origin غريب = 403؛ التبويبات الخمسة؛ الثيمات (auto/light/dark/ocean/amoled/sunset) تغيّر الخلفية فعلاً والحفظ في localStorage بعد إعادة التحميل (theme/lang/tab)؛ اللغات الثماني مع RTL للعربية وLTR للباقي؛ بطاقات KPI وsparkline تُرسم؛ اختبار الفلتر (`path|size`) وregex خاطئ يرجع خطأ؛ حقل حد السرعة؛ Dry-run على مجلدين مؤقتين (3 للنسخ صحيح)؛ Find duplicates (3 ملفات متطابقة صحيحة عبر API مع min_size=1)؛ تحديث/تصدير CSV للسجل؛ الطابور (مهمة بلا مجلد حفظ تفشل برسالة واضحة)؛ إضافة/حذف جدولة؛ حفظ إعدادات الإشعارات واختبار الإرسال مع Telegram معطّل.
- ملاحظة (فحص مستقل ثانٍ): واجهة Find duplicates لا تعرض حقل min_size والحد الافتراضي 1MB، فالملفات الصغيرة لا تظهر كتكرارات إلا عبر API مع min_size=0 (سلوك مقصود لكنه غير ظاهر للمستخدم). الجدولة تبقى محفوظة بعد إعادة تشغيل الخادم. لم يُختبر: Telegram الفعلي، SharePoint/GDrive الحيين، viewport الموبايل.
- أُصلح: شارة نتيجة اختبار الفلتر كانت تبقى بقيمة قديمة عند الخطأ (`static/index.html`)؛ اختبار `test_core` كان يتوقع `/api/health` = `{"ok": True}` بدون version.
- ملاحظات: واجهة "Find duplicates" تستخدم حداً أدنى 1MB افتراضياً بلا حقل لتغييره (ملفات صغيرة لا تظهر)؛ مفتاح i18n `history_placeholder` قديم غير مستخدم.
- لم يُختبر: زر مسح السجل (يعتمد على `confirm()` المعطّل في المتصفح المدمج، فلم يُنفَّذ المسح)؛ إرسال Telegram حقيقي؛ تنفيذ مهام الطابور/الجدولة فعلياً؛ تنزيل حقيقي؛ الشاشات الضيقة/الموبايل.
- بعد الإصلاح: 55 اختباراً ناجحاً.
- تجربة حية (المستخدم، 2026-11-17): أُصلح تجمد الخادم والداشبورد أثناء تحميل ملفات كبيرة على قرص خارجي (كتابة الملف وحفظ manifest في thread بدل حلقة asyncio، أجزاء Range 2–4)، وwatchdog لا يغلق الخادم أثناء مهمة نشطة (120ث)، واختيار ملفات المزامنة (`excluded_paths` في `/api/sync/download`)، وخيارا 8 و10 عاملين. جرّبها المستخدم ونجحت. لا يوجد اختبار آلي لـ `excluded_paths` بعد.
