/*
 * Fiskr — Internationalisation (FR source, EN/DE/ES/ZH/AR cibles).
 *
 * Principe : le HTML et les rendus JS restent ecrits en francais (langue
 * source) ; ce module traduit les nœuds texte et les attributs (placeholder,
 * title, aria-label) par correspondance exacte apres normalisation des
 * blancs, au chargement puis en continu via un MutationObserver (le contenu
 * injecte par app.js est donc traduit aussi). L'arabe passe la page en RTL.
 * Les chaines absentes du dictionnaire restent en francais (jamais de trou).
 */
(function () {
    "use strict";

    const LANGS = { fr: "Français", en: "English", de: "Deutsch", es: "Español", zh: "中文", ar: "العربية" };

    // ---- Dictionnaire : « chaîne française » -> { en, de, es, zh, ar } ----
    const T = {
        // Navigation & header
        "Vue d'ensemble": { en: "Overview", de: "Übersicht", es: "Resumen", zh: "总览", ar: "نظرة عامة" },
        "Gestion des Watchlists": { en: "Watchlist Management", de: "Watchlist-Verwaltung", es: "Gestión de listas", zh: "监控名单管理", ar: "إدارة قوائم المراقبة" },
        "Criblage": { en: "Screening", de: "Screening", es: "Cribado", zh: "筛查", ar: "الفحص" },
        "Alertes": { en: "Alerts", de: "Alarme", es: "Alertas", zh: "警报", ar: "التنبيهات" },
        "Pilotage": { en: "Dashboard & KPIs", de: "Steuerung & KPIs", es: "Indicadores", zh: "指标看板", ar: "لوحة المؤشرات" },
        "Audit": { en: "Audit", de: "Audit", es: "Auditoría", zh: "审计", ar: "التدقيق" },
        "Paramètres": { en: "Settings", de: "Einstellungen", es: "Ajustes", zh: "设置", ar: "الإعدادات" },
        "Utilisateurs": { en: "Users", de: "Benutzer", es: "Usuarios", zh: "用户", ar: "المستخدمون" },
        "Administrateur": { en: "Administrator", de: "Administrator", es: "Administrador", zh: "管理员", ar: "مدير" },
        "Conformité AML/CFT": { en: "AML/CFT Compliance", de: "AML/CFT-Compliance", es: "Cumplimiento AML/CFT", zh: "反洗钱/反恐融资合规", ar: "الامتثال لمكافحة غسل الأموال" },
        "🚪 Déconnexion": { en: "🚪 Log out", de: "🚪 Abmelden", es: "🚪 Cerrar sesión", zh: "🚪 退出登录", ar: "🚪 تسجيل الخروج" },
        "Hash Actif :": { en: "Active hash:", de: "Aktiver Hash:", es: "Hash activo:", zh: "当前哈希：", ar: "التجزئة النشطة:" },
        "Fiskr — Poste de Contrôle Conformité": { en: "Fiskr — Compliance Control Station", de: "Fiskr — Compliance-Leitstand", es: "Fiskr — Puesto de Control de Cumplimiento", zh: "Fiskr — 合规控制台", ar: "Fiskr — محطة مراقبة الامتثال" },
        "Loading...": { en: "Loading...", de: "Laden...", es: "Cargando...", zh: "加载中…", ar: "جارٍ التحميل..." },
        "À traiter": { en: "To handle", de: "Zu bearbeiten", es: "Por tratar", zh: "待处理", ar: "قيد المعالجة" },
        "Moteur opérationnel": { en: "Engine operational", de: "Engine betriebsbereit", es: "Motor operativo", zh: "引擎运行中", ar: "المحرك جاهز" },
        // Accueil
        "📈 Alertes sur 30 jours": { en: "📈 Alerts over 30 days", de: "📈 Alarme über 30 Tage", es: "📈 Alertas en 30 días", zh: "📈 近30天警报", ar: "📈 التنبيهات خلال 30 يومًا" },
        "📊 Fiches en production par liste": { en: "📊 Production records per list", de: "📊 Produktive Einträge je Liste", es: "📊 Fichas en producción por lista", zh: "📊 各名单在产记录", ar: "📊 السجلات المنشورة حسب القائمة" },
        "🗂 Répartition des alertes": { en: "🗂 Alert breakdown", de: "🗂 Alarmverteilung", es: "🗂 Distribución de alertas", zh: "🗂 警报分布", ar: "🗂 توزيع التنبيهات" },
        "⏳ À traiter en priorité": { en: "⏳ Handle first", de: "⏳ Zuerst bearbeiten", es: "⏳ Prioritarias", zh: "⏳ 优先处理", ar: "⏳ أولوية المعالجة" },
        "🔄 Dernière synchronisation": { en: "🔄 Last synchronization", de: "🔄 Letzte Synchronisation", es: "🔄 Última sincronización", zh: "🔄 最近同步", ar: "🔄 آخر مزامنة" },
        // Watchlists
        "Watchlist Active": { en: "Active Watchlist", de: "Aktive Watchlist", es: "Lista activa", zh: "当前名单", ar: "القائمة النشطة" },
        "Import de Fichiers": { en: "File Import", de: "Datei-Import", es: "Importación de archivos", zh: "文件导入", ar: "استيراد الملفات" },
        "Snapshots & Comparateur": { en: "Snapshots & Comparator", de: "Snapshots & Vergleich", es: "Instantáneas y comparador", zh: "快照与比对", ar: "اللقطات والمقارنة" },
        "Ajout Manuel": { en: "Manual Entry", de: "Manuelle Erfassung", es: "Alta manual", zh: "手动添加", ar: "إضافة يدوية" },
        "Sources Automatiques": { en: "Automatic Sources", de: "Automatische Quellen", es: "Fuentes automáticas", zh: "自动数据源", ar: "المصادر التلقائية" },
        "Homologation": { en: "Approval", de: "Freigabe", es: "Homologación", zh: "审批", ar: "الاعتماد" },
        "Listés — Base de Données (en direct)": { en: "Listed parties — Database (live)", de: "Gelistete — Datenbank (live)", es: "Listados — Base de datos (en vivo)", zh: "列名对象 — 数据库（实时）", ar: "المدرجون — قاعدة البيانات (مباشر)" },
        "En production": { en: "In production", de: "Produktiv", es: "En producción", zh: "在产", ar: "قيد الإنتاج" },
        "Tous les statuts": { en: "All statuses", de: "Alle Status", es: "Todos los estados", zh: "全部状态", ar: "كل الحالات" },
        "En attente d'homologation": { en: "Pending approval", de: "Freigabe ausstehend", es: "Pendiente de homologación", zh: "待审批", ar: "بانتظار الاعتماد" },
        "Remplacées": { en: "Superseded", de: "Ersetzt", es: "Sustituidas", zh: "已替换", ar: "مستبدلة" },
        "Rejetées": { en: "Rejected", de: "Abgelehnt", es: "Rechazadas", zh: "已拒绝", ar: "مرفوضة" },
        "Exclues": { en: "Excluded", de: "Ausgeschlossen", es: "Excluidas", zh: "已排除", ar: "مستبعدة" },
        "⬇ CSV": { en: "⬇ CSV", de: "⬇ CSV", es: "⬇ CSV", zh: "⬇ CSV", ar: "⬇ CSV" },
        "Liste": { en: "List", de: "Liste", es: "Lista", zh: "名单", ar: "القائمة" },
        "Statut": { en: "Status", de: "Status", es: "Estado", zh: "状态", ar: "الحالة" },
        "Type d'entité": { en: "Entity type", de: "Entitätstyp", es: "Tipo de entidad", zh: "实体类型", ar: "نوع الكيان" },
        "Nom Principal / Raison Sociale": { en: "Primary name / Company name", de: "Hauptname / Firmenname", es: "Nombre principal / Razón social", zh: "主要名称/公司名称", ar: "الاسم الرئيسي / اسم الشركة" },
        "Pays Rattachés": { en: "Linked countries", de: "Zugeordnete Länder", es: "Países vinculados", zh: "关联国家", ar: "الدول المرتبطة" },
        "État / Genre": { en: "State / Gender", de: "Status / Geschlecht", es: "Estado / Género", zh: "状态/性别", ar: "الحالة / الجنس" },
        "Importer un Instantané (Snapshot)": { en: "Import a Snapshot", de: "Snapshot importieren", es: "Importar una instantánea", zh: "导入快照", ar: "استيراد لقطة" },
        "Type de Fichier *": { en: "File type *", de: "Dateityp *", es: "Tipo de archivo *", zh: "文件类型 *", ar: "نوع الملف *" },
        "Fichier *": { en: "File *", de: "Datei *", es: "Archivo *", zh: "文件 *", ar: "الملف *" },
        "Délimiteur CSV": { en: "CSV delimiter", de: "CSV-Trennzeichen", es: "Delimitador CSV", zh: "CSV 分隔符", ar: "فاصل CSV" },
        "Charger & Archiver": { en: "Load & Archive", de: "Laden & Archivieren", es: "Cargar y archivar", zh: "加载并归档", ar: "تحميل وأرشفة" },
        "Comparateur de Versions (Delta Engine)": { en: "Version Comparator (Delta Engine)", de: "Versionsvergleich (Delta Engine)", es: "Comparador de versiones (Delta Engine)", zh: "版本比对（Delta 引擎）", ar: "مقارن الإصدارات (محرك الفروق)" },
        "Sélectionnez un snapshot...": { en: "Select a snapshot...", de: "Snapshot wählen...", es: "Seleccione una instantánea...", zh: "选择快照…", ar: "اختر لقطة..." },
        "Comparer les versions": { en: "Compare versions", de: "Versionen vergleichen", es: "Comparar versiones", zh: "比较版本", ar: "قارن الإصدارات" },
        "Historique des Snapshots Archives": { en: "Archived snapshot history", de: "Archivierte Snapshots", es: "Histórico de instantáneas", zh: "快照归档历史", ar: "سجل اللقطات المؤرشفة" },
        "🗑️ Purger les imports erronés": { en: "🗑️ Purge failed imports", de: "🗑️ Fehlimporte löschen", es: "🗑️ Purgar importaciones erróneas", zh: "🗑️ 清除错误导入", ar: "🗑️ حذف الاستيرادات الخاطئة" },
        "Date d'Import": { en: "Import date", de: "Importdatum", es: "Fecha de importación", zh: "导入日期", ar: "تاريخ الاستيراد" },
        "Nom du Fichier": { en: "File name", de: "Dateiname", es: "Nombre del archivo", zh: "文件名", ar: "اسم الملف" },
        "Type": { en: "Type", de: "Typ", es: "Tipo", zh: "类型", ar: "النوع" },
        "Lignes": { en: "Rows", de: "Zeilen", es: "Filas", zh: "行数", ar: "الصفوف" },
        "Ajouts (ADDED)": { en: "Additions (ADDED)", de: "Zugänge (ADDED)", es: "Altas (ADDED)", zh: "新增（ADDED）", ar: "إضافات (ADDED)" },
        "Suppressions (REMOVED)": { en: "Removals (REMOVED)", de: "Löschungen (REMOVED)", es: "Bajas (REMOVED)", zh: "删除（REMOVED）", ar: "حذف (REMOVED)" },
        "Modifications (MODIFIED)": { en: "Changes (MODIFIED)", de: "Änderungen (MODIFIED)", es: "Cambios (MODIFIED)", zh: "修改（MODIFIED）", ar: "تعديلات (MODIFIED)" },
        "Nom / Label": { en: "Name / Label", de: "Name / Label", es: "Nombre / Etiqueta", zh: "名称/标签", ar: "الاسم / التسمية" },
        "Attributs Modifiés": { en: "Changed attributes", de: "Geänderte Attribute", es: "Atributos modificados", zh: "已修改属性", ar: "السمات المعدلة" },
        "Type d'Entité *": { en: "Entity type *", de: "Entitätstyp *", es: "Tipo de entidad *", zh: "实体类型 *", ar: "نوع الكيان *" },
        "Individu (PP)": { en: "Individual (PP)", de: "Natürliche Person (PP)", es: "Persona física (PP)", zh: "自然人（PP）", ar: "فرد (PP)" },
        "Entité / Personne Morale (PM)": { en: "Entity / Legal person (PM)", de: "Juristische Person (PM)", es: "Persona jurídica (PM)", zh: "法人（PM）", ar: "كيان / شخص اعتباري (PM)" },
        "Navire (Vessel)": { en: "Vessel", de: "Schiff (Vessel)", es: "Buque (Vessel)", zh: "船舶", ar: "سفينة" },
        "Autre": { en: "Other", de: "Sonstiges", es: "Otro", zh: "其他", ar: "أخرى" },
        "Nom Principal / Raison Sociale *": { en: "Primary name / Company name *", de: "Hauptname / Firmenname *", es: "Nombre principal / Razón social *", zh: "主要名称/公司名称 *", ar: "الاسم الرئيسي / اسم الشركة *" },
        "Prénom": { en: "First name", de: "Vorname", es: "Nombre", zh: "名", ar: "الاسم الأول" },
        "Nom de Famille": { en: "Last name", de: "Nachname", es: "Apellido", zh: "姓", ar: "اسم العائلة" },
        "Nom de Jeune Fille": { en: "Maiden name", de: "Geburtsname", es: "Apellido de soltera", zh: "婚前姓", ar: "اسم ما قبل الزواج" },
        "Genre": { en: "Gender", de: "Geschlecht", es: "Género", zh: "性别", ar: "الجنس" },
        "Non spécifié (U)": { en: "Unspecified (U)", de: "Nicht angegeben (U)", es: "No especificado (U)", zh: "未指定（U）", ar: "غير محدد (U)" },
        "Masculin (M)": { en: "Male (M)", de: "Männlich (M)", es: "Masculino (M)", zh: "男（M）", ar: "ذكر (M)" },
        "Féminin (F)": { en: "Female (F)", de: "Weiblich (F)", es: "Femenino (F)", zh: "女（F）", ar: "أنثى (F)" },
        "Lieu de Naissance": { en: "Place of birth", de: "Geburtsort", es: "Lugar de nacimiento", zh: "出生地", ar: "مكان الولادة" },
        "Adresse": { en: "Address", de: "Adresse", es: "Dirección", zh: "地址", ar: "العنوان" },
        "Ville": { en: "City", de: "Stadt", es: "Ciudad", zh: "城市", ar: "المدينة" },
        "État / Région": { en: "State / Region", de: "Bundesland / Region", es: "Estado / Región", zh: "州/地区", ar: "الولاية / المنطقة" },
        "Pays": { en: "Country", de: "Land", es: "País", zh: "国家", ar: "البلد" },
        "Origine / Source": { en: "Origin / Source", de: "Herkunft / Quelle", es: "Origen / Fuente", zh: "来源", ar: "الأصل / المصدر" },
        "Fonction / Désignation": { en: "Function / Designation", de: "Funktion / Bezeichnung", es: "Función / Designación", zh: "职务/称号", ar: "الوظيفة / الصفة" },
        "Motifs de la Désignation": { en: "Designation reasons", de: "Gründe der Listung", es: "Motivos de la designación", zh: "列名理由", ar: "أسباب الإدراج" },
        "Informations Additionnelles": { en: "Additional information", de: "Zusätzliche Informationen", es: "Información adicional", zh: "附加信息", ar: "معلومات إضافية" },
        "Ajouter à la Watchlist Active": { en: "Add to active watchlist", de: "Zur aktiven Watchlist hinzufügen", es: "Añadir a la lista activa", zh: "加入当前名单", ar: "أضف إلى القائمة النشطة" },
        "Collecteurs de Sources Officielles": { en: "Official source collectors", de: "Sammler offizieller Quellen", es: "Colectores de fuentes oficiales", zh: "官方数据源采集器", ar: "جامعو المصادر الرسمية" },
        "Synchroniser maintenant": { en: "Synchronize now", de: "Jetzt synchronisieren", es: "Sincronizar ahora", zh: "立即同步", ar: "زامن الآن" },
        "Source": { en: "Source", de: "Quelle", es: "Fuente", zh: "来源", ar: "المصدر" },
        "Expression cron": { en: "Cron expression", de: "Cron-Ausdruck", es: "Expresión cron", zh: "Cron 表达式", ar: "تعبير cron" },
        "Prochaine exécution": { en: "Next run", de: "Nächste Ausführung", es: "Próxima ejecución", zh: "下次执行", ar: "التشغيل القادم" },
        "Enregistrer la planification": { en: "Save schedule", de: "Zeitplan speichern", es: "Guardar planificación", zh: "保存计划", ar: "احفظ الجدولة" },
        "Rapports de Synchronisation": { en: "Synchronization reports", de: "Synchronisationsberichte", es: "Informes de sincronización", zh: "同步报告", ar: "تقارير المزامنة" },
        "Date": { en: "Date", de: "Datum", es: "Fecha", zh: "日期", ar: "التاريخ" },
        "Email": { en: "Email", de: "E-Mail", es: "Correo", zh: "邮件", ar: "البريد" },
        "Snapshots en Attente d'Homologation": { en: "Snapshots pending approval", de: "Snapshots mit ausstehender Freigabe", es: "Instantáneas pendientes de homologación", zh: "待审批快照", ar: "لقطات بانتظار الاعتماد" },
        "Fichier": { en: "File", de: "Datei", es: "Archivo", zh: "文件", ar: "الملف" },
        "Fiches": { en: "Records", de: "Einträge", es: "Fichas", zh: "记录", ar: "السجلات" },
        "Exclusions": { en: "Exclusions", de: "Ausschlüsse", es: "Exclusiones", zh: "排除项", ar: "الاستثناءات" },
        "Action": { en: "Action", de: "Aktion", es: "Acción", zh: "操作", ar: "إجراء" },
        "Examen du Snapshot": { en: "Snapshot review", de: "Snapshot-Prüfung", es: "Examen de la instantánea", zh: "快照审查", ar: "مراجعة اللقطة" },
        "Rechercher": { en: "Search", de: "Suchen", es: "Buscar", zh: "搜索", ar: "بحث" },
        "Exclure la sélection": { en: "Exclude selection", de: "Auswahl ausschließen", es: "Excluir selección", zh: "排除所选", ar: "استبعد المحدد" },
        "Réintégrer la sélection": { en: "Reinstate selection", de: "Auswahl wiederaufnehmen", es: "Reintegrar selección", zh: "恢复所选", ar: "أعد المحدد" },
        "Nom Principal": { en: "Primary name", de: "Hauptname", es: "Nombre principal", zh: "主要名称", ar: "الاسم الرئيسي" },
        "Exclusion / Justification": { en: "Exclusion / Justification", de: "Ausschluss / Begründung", es: "Exclusión / Justificación", zh: "排除/理由", ar: "الاستبعاد / التبرير" },
        "Panel de pseudo-clients": { en: "Pseudo-client panel", de: "Pseudo-Kunden-Panel", es: "Panel de pseudoclientes", zh: "模拟客户面板", ar: "لوحة عملاء وهميين" },
        "⚙️ Générer un panel": { en: "⚙️ Generate panel", de: "⚙️ Panel erzeugen", es: "⚙️ Generar panel", zh: "⚙️ 生成面板", ar: "⚙️ إنشاء اللوحة" },
        "▶ Lancer le cahier de tests": { en: "▶ Run test book", de: "▶ Testheft ausführen", es: "▶ Ejecutar cuaderno de pruebas", zh: "▶ 运行测试集", ar: "▶ شغّل دفتر الاختبارات" },
        "✅ Approuver & Mettre en Production": { en: "✅ Approve & Promote to production", de: "✅ Freigeben & Produktiv setzen", es: "✅ Aprobar y poner en producción", zh: "✅ 批准并投产", ar: "✅ اعتماد ونشر للإنتاج" },
        "⛔ Rejeter": { en: "⛔ Reject", de: "⛔ Ablehnen", es: "⛔ Rechazar", zh: "⛔ 拒绝", ar: "⛔ رفض" },
        // Criblage
        "Criblage Temps Réel": { en: "Real-time Screening", de: "Echtzeit-Screening", es: "Cribado en tiempo real", zh: "实时筛查", ar: "فحص فوري" },
        "Screening de Masse (Batch)": { en: "Mass Screening (Batch)", de: "Massen-Screening (Batch)", es: "Cribado masivo (Batch)", zh: "批量筛查", ar: "فحص شامل (دفعات)" },
        "Filtrage Transactionnel (ISO 20022)": { en: "Transaction Filtering (ISO 20022)", de: "Transaktionsfilterung (ISO 20022)", es: "Filtrado transaccional (ISO 20022)", zh: "交易过滤（ISO 20022）", ar: "تصفية المعاملات (ISO 20022)" },
        "Saisie du Profil Client": { en: "Client profile entry", de: "Kundenprofil erfassen", es: "Captura del perfil del cliente", zh: "客户信息录入", ar: "إدخال ملف العميل" },
        "Prénom *": { en: "First name *", de: "Vorname *", es: "Nombre *", zh: "名 *", ar: "الاسم الأول *" },
        "Nom de Famille *": { en: "Last name *", de: "Nachname *", es: "Apellido *", zh: "姓 *", ar: "اسم العائلة *" },
        "Date de Naissance (DOB)": { en: "Date of birth (DOB)", de: "Geburtsdatum (DOB)", es: "Fecha de nacimiento (DOB)", zh: "出生日期（DOB）", ar: "تاريخ الميلاد" },
        "Raison Sociale / Nom PM *": { en: "Company name (legal person) *", de: "Firmenname (jur. Person) *", es: "Razón social (PM) *", zh: "公司名称（法人）*", ar: "اسم الشركة (اعتباري) *" },
        "Lancer le criblage": { en: "Run screening", de: "Screening starten", es: "Lanzar cribado", zh: "开始筛查", ar: "ابدأ الفحص" },
        "En attente de saisie": { en: "Waiting for input", de: "Warte auf Eingabe", es: "A la espera de datos", zh: "等待输入", ar: "بانتظار الإدخال" },
        "Résultats du Screening": { en: "Screening results", de: "Screening-Ergebnisse", es: "Resultados del cribado", zh: "筛查结果", ar: "نتائج الفحص" },
        "Score Final": { en: "Final score", de: "Endscore", es: "Puntuación final", zh: "最终得分", ar: "النتيجة النهائية" },
        "Score de base textuel": { en: "Textual base score", de: "Textueller Basisscore", es: "Puntuación base textual", zh: "文本基础得分", ar: "النتيجة النصية الأساسية" },
        "Candidats évalués": { en: "Candidates evaluated", de: "Geprüfte Kandidaten", es: "Candidatos evaluados", zh: "已评估候选", ar: "المرشحون المقيمون" },
        "Clés de blocking": { en: "Blocking keys", de: "Blocking-Schlüssel", es: "Claves de bloqueo", zh: "阻断键", ar: "مفاتيح الحجب" },
        "Validé": { en: "Passed", de: "Bestanden", es: "Validado", zh: "通过", ar: "مقبول" },
        "⚡ HARD MATCH déclenché !": { en: "⚡ HARD MATCH triggered!", de: "⚡ HARD MATCH ausgelöst!", es: "⚡ ¡HARD MATCH activado!", zh: "⚡ 触发精确命中！", ar: "⚡ تطابق مؤكد!" },
        "Nom Client Apparié :": { en: "Matched client name:", de: "Zugeordneter Kundenname:", es: "Nombre de cliente emparejado:", zh: "匹配客户姓名：", ar: "اسم العميل المطابق:" },
        "Nom Watchlist Apparié :": { en: "Matched watchlist name:", de: "Zugeordneter Listenname:", es: "Nombre de lista emparejado:", zh: "匹配名单名称：", ar: "اسم القائمة المطابق:" },
        "Ajustements Contextuels": { en: "Contextual adjustments", de: "Kontextanpassungen", es: "Ajustes contextuales", zh: "上下文调整", ar: "تعديلات سياقية" },
        "Géographie (Pays)": { en: "Geography (country)", de: "Geografie (Land)", es: "Geografía (país)", zh: "地理（国家）", ar: "الجغرافيا (البلد)" },
        "🚀 Lancer la campagne": { en: "🚀 Launch campaign", de: "🚀 Kampagne starten", es: "🚀 Lanzar campaña", zh: "🚀 启动批次", ar: "🚀 أطلق الحملة" },
        "Nom": { en: "Name", de: "Name", es: "Nombre", zh: "名称", ar: "الاسم" },
        "Origine": { en: "Origin", de: "Herkunft", es: "Origen", zh: "来源", ar: "الأصل" },
        "Progression": { en: "Progress", de: "Fortschritt", es: "Progreso", zh: "进度", ar: "التقدم" },
        "Rejets": { en: "Rejects", de: "Zurückweisungen", es: "Rechazos", zh: "拒绝数", ar: "المرفوضات" },
        "Lancée le": { en: "Launched on", de: "Gestartet am", es: "Lanzada el", zh: "启动时间", ar: "أطلقت في" },
        "Actions": { en: "Actions", de: "Aktionen", es: "Acciones", zh: "操作", ar: "الإجراءات" },
        "Lancer le Batch Screening": { en: "Run batch screening", de: "Batch-Screening starten", es: "Lanzar cribado por lotes", zh: "运行批量筛查", ar: "شغّل الفحص الدفعي" },
        "Charger Exemples": { en: "Load samples", de: "Beispiele laden", es: "Cargar ejemplos", zh: "载入示例", ar: "حمّل أمثلة" },
        "ID Client": { en: "Client ID", de: "Kunden-ID", es: "ID de cliente", zh: "客户ID", ar: "معرّف العميل" },
        "Nom Client": { en: "Client name", de: "Kundenname", es: "Nombre del cliente", zh: "客户姓名", ar: "اسم العميل" },
        "Filtrage Transactionnel ISO 20022": { en: "ISO 20022 Transaction Filtering", de: "ISO-20022-Transaktionsfilterung", es: "Filtrado transaccional ISO 20022", zh: "ISO 20022 交易过滤", ar: "تصفية معاملات ISO 20022" },
        "Message de paiement (XML ISO 20022)": { en: "Payment message (ISO 20022 XML)", de: "Zahlungsnachricht (ISO-20022-XML)", es: "Mensaje de pago (XML ISO 20022)", zh: "支付报文（ISO 20022 XML）", ar: "رسالة الدفع (XML ISO 20022)" },
        "Filtrer le paiement": { en: "Filter payment", de: "Zahlung filtern", es: "Filtrar el pago", zh: "过滤支付", ar: "صفِّ الدفعة" },
        "Partie": { en: "Party", de: "Partei", es: "Parte", zh: "当事方", ar: "الطرف" },
        "Rôle(s)": { en: "Role(s)", de: "Rolle(n)", es: "Rol(es)", zh: "角色", ar: "الأدوار" },
        "Score": { en: "Score", de: "Score", es: "Puntuación", zh: "得分", ar: "النتيجة" },
        "Meilleur match": { en: "Best match", de: "Bester Treffer", es: "Mejor coincidencia", zh: "最佳匹配", ar: "أفضل تطابق" },
        // Alertes
        "🧍 Criblage Clients": { en: "🧍 Client Screening", de: "🧍 Kunden-Screening", es: "🧍 Cribado de clientes", zh: "🧍 客户筛查", ar: "🧍 فحص العملاء" },
        "💸 Filtrage Transactionnel": { en: "💸 Transaction Filtering", de: "💸 Transaktionsfilterung", es: "💸 Filtrado transaccional", zh: "💸 交易过滤", ar: "💸 تصفية المعاملات" },
        "Liste Blanche": { en: "Whitelist", de: "Whitelist", es: "Lista blanca", zh: "白名单", ar: "القائمة البيضاء" },
        "🔑 Blocking Keys": { en: "🔑 Blocking Keys", de: "🔑 Blocking-Schlüssel", es: "🔑 Claves de bloqueo", zh: "🔑 阻断键", ar: "🔑 مفاتيح الحجب" },
        "⚖️ Règles Faux Positifs": { en: "⚖️ False-Positive Rules", de: "⚖️ Fehlalarm-Regeln", es: "⚖️ Reglas de falsos positivos", zh: "⚖️ 误报规则", ar: "⚖️ قواعد الإنذارات الكاذبة" },
        "Alertes de Criblage — Données Clients": { en: "Screening alerts — client data", de: "Screening-Alarme — Kundendaten", es: "Alertas de cribado — datos de clientes", zh: "筛查警报 — 客户数据", ar: "تنبيهات الفحص — بيانات العملاء" },
        "Alertes de Filtrage — Transactions (ISO 20022)": { en: "Filtering alerts — transactions (ISO 20022)", de: "Filter-Alarme — Transaktionen (ISO 20022)", es: "Alertas de filtrado — transacciones (ISO 20022)", zh: "过滤警报 — 交易（ISO 20022）", ar: "تنبيهات التصفية — المعاملات (ISO 20022)" },
        "Toutes": { en: "All", de: "Alle", es: "Todas", zh: "全部", ar: "الكل" },
        "À valider (4-yeux)": { en: "To validate (4-eyes)", de: "Zu validieren (4-Augen)", es: "Por validar (4 ojos)", zh: "待复核（四眼）", ar: "بانتظار التحقق (أربع أعين)" },
        "Closes": { en: "Closed", de: "Geschlossen", es: "Cerradas", zh: "已关闭", ar: "مغلقة" },
        "Clôturées par règle": { en: "Closed by rule", de: "Per Regel geschlossen", es: "Cerradas por regla", zh: "按规则关闭", ar: "أُغلقت بقاعدة" },
        "Toutes priorités": { en: "All priorities", de: "Alle Prioritäten", es: "Todas las prioridades", zh: "全部优先级", ar: "كل الأولويات" },
        "Critique": { en: "Critical", de: "Kritisch", es: "Crítica", zh: "严重", ar: "حرجة" },
        "Haute": { en: "High", de: "Hoch", es: "Alta", zh: "高", ar: "مرتفعة" },
        "Moyenne": { en: "Medium", de: "Mittel", es: "Media", zh: "中", ar: "متوسطة" },
        "Basse": { en: "Low", de: "Niedrig", es: "Baja", zh: "低", ar: "منخفضة" },
        "Vues…": { en: "Views…", de: "Ansichten…", es: "Vistas…", zh: "视图…", ar: "طرق العرض…" },
        "📌 M'assigner la sélection": { en: "📌 Assign selection to me", de: "📌 Auswahl mir zuweisen", es: "📌 Asignarme la selección", zh: "📌 指派给我", ar: "📌 أسند المحدد إلي" },
        "Priorité…": { en: "Priority…", de: "Priorität…", es: "Prioridad…", zh: "优先级…", ar: "الأولوية…" },
        "Appliquer la priorité": { en: "Apply priority", de: "Priorität anwenden", es: "Aplicar prioridad", zh: "应用优先级", ar: "طبّق الأولوية" },
        "✕ Vider la sélection": { en: "✕ Clear selection", de: "✕ Auswahl leeren", es: "✕ Vaciar selección", zh: "✕ 清空选择", ar: "✕ أفرغ التحديد" },
        "Priorité": { en: "Priority", de: "Priorität", es: "Prioridad", zh: "优先级", ar: "الأولوية" },
        "Client": { en: "Client", de: "Kunde", es: "Cliente", zh: "客户", ar: "العميل" },
        "Listé": { en: "Listed party", de: "Gelisteter", es: "Listado", zh: "列名对象", ar: "المدرج" },
        "Assignée à": { en: "Assigned to", de: "Zugewiesen an", es: "Asignada a", zh: "指派给", ar: "مسندة إلى" },
        "Message / Partie": { en: "Message / Party", de: "Nachricht / Partei", es: "Mensaje / Parte", zh: "报文/当事方", ar: "الرسالة / الطرف" },
        "Enregistrer": { en: "Save", de: "Speichern", es: "Guardar", zh: "保存", ar: "حفظ" },
        "Enregistrer (recharge le cache)": { en: "Save (reloads cache)", de: "Speichern (Cache wird neu geladen)", es: "Guardar (recarga la caché)", zh: "保存（重载缓存）", ar: "احفظ (يعيد تحميل الذاكرة)" },
        "Canal :": { en: "Channel:", de: "Kanal:", es: "Canal:", zh: "通道：", ar: "القناة:" },
        "+ Nouvelle règle": { en: "+ New rule", de: "+ Neue Regel", es: "+ Nueva regla", zh: "+ 新规则", ar: "+ قاعدة جديدة" },
        "Ordre": { en: "Order", de: "Reihenfolge", es: "Orden", zh: "顺序", ar: "الترتيب" },
        "Version": { en: "Version", de: "Version", es: "Versión", zh: "版本", ar: "الإصدار" },
        "Hits": { en: "Hits", de: "Treffer", es: "Aciertos", zh: "命中", ar: "إصابات" },
        "Liste Blanche (paires client × listé)": { en: "Whitelist (client × listed pairs)", de: "Whitelist (Kunde × Gelisteter)", es: "Lista blanca (pares cliente × listado)", zh: "白名单（客户×列名对）", ar: "القائمة البيضاء (أزواج عميل × مدرج)" },
        "Justification": { en: "Justification", de: "Begründung", es: "Justificación", zh: "理由", ar: "التبرير" },
        "Créée par": { en: "Created by", de: "Erstellt von", es: "Creada por", zh: "创建人", ar: "أنشأها" },
        "Expiration": { en: "Expiry", de: "Ablauf", es: "Expiración", zh: "到期", ar: "الانتهاء" },
        "État": { en: "State", de: "Zustand", es: "Estado", zh: "状态", ar: "الوضع" },
        // Pilotage
        "Indicateurs de Pilotage du Dispositif": { en: "Programme steering indicators", de: "Steuerungskennzahlen", es: "Indicadores de pilotaje", zh: "体系运行指标", ar: "مؤشرات قيادة المنظومة" },
        "Listes en production (fiches par type)": { en: "Lists in production (records per type)", de: "Produktive Listen (Einträge je Typ)", es: "Listas en producción (fichas por tipo)", zh: "在产名单（按类型）", ar: "القوائم المنشورة (سجلات حسب النوع)" },
        "Dernières synchronisations": { en: "Latest synchronizations", de: "Letzte Synchronisationen", es: "Últimas sincronizaciones", zh: "最近同步", ar: "آخر المزامنات" },
        "Delta": { en: "Delta", de: "Delta", es: "Delta", zh: "增量", ar: "الفرق" },
        "Traitement par analyste (alertes décidées)": { en: "Per-analyst handling (decided alerts)", de: "Bearbeitung je Analyst (entschiedene Alarme)", es: "Tratamiento por analista (alertas decididas)", zh: "分析员处理量（已决警报）", ar: "معالجة حسب المحلل (تنبيهات مقررة)" },
        "Analyste": { en: "Analyst", de: "Analyst", es: "Analista", zh: "分析员", ar: "المحلل" },
        "Décisions": { en: "Decisions", de: "Entscheidungen", es: "Decisiones", zh: "决定数", ar: "القرارات" },
        "Délai moyen": { en: "Average delay", de: "Durchschnittsdauer", es: "Plazo medio", zh: "平均时长", ar: "متوسط المدة" },
        "Règles anti-faux positifs actives (efficacité)": { en: "Active false-positive rules (efficiency)", de: "Aktive Fehlalarm-Regeln (Wirksamkeit)", es: "Reglas antifalsos positivos activas (eficacia)", zh: "在用误报规则（效率）", ar: "قواعد الإنذارات الكاذبة النشطة (الفعالية)" },
        "Règle": { en: "Rule", de: "Regel", es: "Regla", zh: "规则", ar: "القاعدة" },
        "Canal": { en: "Channel", de: "Kanal", es: "Canal", zh: "通道", ar: "القناة" },
        "Alertes closes": { en: "Closed alerts", de: "Geschlossene Alarme", es: "Alertas cerradas", zh: "已关闭警报", ar: "تنبيهات مغلقة" },
        "👥 Charge de Travail des Analystes": { en: "👥 Analyst Workload", de: "👥 Arbeitslast der Analysten", es: "👥 Carga de trabajo de analistas", zh: "👥 分析员工作量", ar: "👥 عبء عمل المحللين" },
        "Tous canaux": { en: "All channels", de: "Alle Kanäle", es: "Todos los canales", zh: "全部通道", ar: "كل القنوات" },
        "Criblage clients": { en: "Client screening", de: "Kunden-Screening", es: "Cribado de clientes", zh: "客户筛查", ar: "فحص العملاء" },
        "Filtrage transactionnel": { en: "Transaction filtering", de: "Transaktionsfilterung", es: "Filtrado transaccional", zh: "交易过滤", ar: "تصفية المعاملات" },
        "Ouvertes": { en: "Open", de: "Offen", es: "Abiertas", zh: "未结", ar: "مفتوحة" },
        "⏰ En retard": { en: "⏰ Overdue", de: "⏰ Überfällig", es: "⏰ Atrasadas", zh: "⏰ 逾期", ar: "⏰ متأخرة" },
        "Prochaine échéance": { en: "Next deadline", de: "Nächste Frist", es: "Próximo vencimiento", zh: "最近期限", ar: "الأجل القادم" },
        "4-yeux": { en: "4-eyes", de: "4-Augen", es: "4 ojos", zh: "四眼", ar: "أربع أعين" },
        "📄 Rapport d'Activité (période)": { en: "📄 Activity Report (period)", de: "📄 Tätigkeitsbericht (Zeitraum)", es: "📄 Informe de actividad (período)", zh: "📄 活动报告（期间）", ar: "📄 تقرير النشاط (فترة)" },
        "Du": { en: "From", de: "Von", es: "Del", zh: "从", ar: "من" },
        "au": { en: "to", de: "bis", es: "al", zh: "至", ar: "إلى" },
        "Afficher": { en: "Show", de: "Anzeigen", es: "Mostrar", zh: "显示", ar: "عرض" },
        "🖨 Imprimer": { en: "🖨 Print", de: "🖨 Drucken", es: "🖨 Imprimir", zh: "🖨 打印", ar: "🖨 طباعة" },
        "Section": { en: "Section", de: "Abschnitt", es: "Sección", zh: "部分", ar: "القسم" },
        "Indicateur": { en: "Indicator", de: "Kennzahl", es: "Indicador", zh: "指标", ar: "المؤشر" },
        "Valeur": { en: "Value", de: "Wert", es: "Valor", zh: "数值", ar: "القيمة" },
        "Choisissez une période puis « Afficher ».": { en: "Choose a period, then \"Show\".", de: "Zeitraum wählen, dann „Anzeigen“.", es: "Elija un período y pulse «Mostrar».", zh: "选择期间后点击“显示”。", ar: "اختر فترة ثم «عرض»." },
        // Audit
        "Décisions de Criblage": { en: "Screening decisions", de: "Screening-Entscheidungen", es: "Decisiones de cribado", zh: "筛查决定", ar: "قرارات الفحص" },
        "Actions d'Administration": { en: "Administration actions", de: "Administrationsaktionen", es: "Acciones de administración", zh: "管理操作", ar: "إجراءات الإدارة" },
        "Toutes les décisions": { en: "All decisions", de: "Alle Entscheidungen", es: "Todas las decisiones", zh: "全部决定", ar: "كل القرارات" },
        "Aucun match": { en: "No match", de: "Kein Treffer", es: "Sin coincidencia", zh: "无匹配", ar: "لا تطابق" },
        "Liste blanche": { en: "Whitelist", de: "Whitelist", es: "Lista blanca", zh: "白名单", ar: "القائمة البيضاء" },
        "Horodatage": { en: "Timestamp", de: "Zeitstempel", es: "Marca de tiempo", zh: "时间戳", ar: "الطابع الزمني" },
        "Fiche Matchée": { en: "Matched record", de: "Getroffener Eintrag", es: "Ficha coincidente", zh: "匹配记录", ar: "السجل المطابق" },
        "Détail": { en: "Detail", de: "Detail", es: "Detalle", zh: "详情", ar: "التفاصيل" },
        "Journal des Actions d'Administration": { en: "Administration action log", de: "Protokoll der Admin-Aktionen", es: "Registro de acciones de administración", zh: "管理操作日志", ar: "سجل إجراءات الإدارة" },
        "Utilisateur": { en: "User", de: "Benutzer", es: "Usuario", zh: "用户", ar: "المستخدم" },
        "Cible": { en: "Target", de: "Ziel", es: "Objetivo", zh: "对象", ar: "الهدف" },
        "Détail (avant → après)": { en: "Detail (before → after)", de: "Detail (vorher → nachher)", es: "Detalle (antes → después)", zh: "详情（前→后）", ar: "التفاصيل (قبل ← بعد)" },
        // Paramètres
        "Réglages de Gouvernance (Admin)": { en: "Governance settings (Admin)", de: "Governance-Einstellungen (Admin)", es: "Ajustes de gobernanza (Admin)", zh: "治理设置（管理员）", ar: "إعدادات الحوكمة (مدير)" },
        "Homologation obligatoire avant mise en production": { en: "Approval required before production", de: "Freigabe vor Produktivsetzung erforderlich", es: "Homologación obligatoria antes de producción", zh: "投产前必须审批", ar: "الاعتماد إلزامي قبل النشر" },
        "⏱ SLA de traitement des alertes": { en: "⏱ Alert handling SLA", de: "⏱ SLA der Alarmbearbeitung", es: "⏱ SLA de tratamiento de alertas", zh: "⏱ 警报处理SLA", ar: "⏱ اتفاقية مستوى معالجة التنبيهات" },
        "🔔 Notifications métier (email / webhooks)": { en: "🔔 Business notifications (email / webhooks)", de: "🔔 Fachliche Benachrichtigungen (E-Mail / Webhooks)", es: "🔔 Notificaciones de negocio (correo / webhooks)", zh: "🔔 业务通知（邮件/Webhook）", ar: "🔔 إشعارات الأعمال (بريد / Webhooks)" },
        "Nouvelle alerte créée": { en: "New alert created", de: "Neuer Alarm erstellt", es: "Nueva alerta creada", zh: "新警报创建", ar: "تنبيه جديد" },
        "Décision d'alerte en attente de validation 4-yeux": { en: "Alert decision pending 4-eyes validation", de: "Alarm-Entscheidung wartet auf 4-Augen-Prüfung", es: "Decisión de alerta pendiente de validación 4 ojos", zh: "警报决定待四眼复核", ar: "قرار تنبيه بانتظار تحقق أربع أعين" },
        "Snapshot en attente d'homologation": { en: "Snapshot pending approval", de: "Snapshot wartet auf Freigabe", es: "Instantánea pendiente de homologación", zh: "快照待审批", ar: "لقطة بانتظار الاعتماد" },
        "Échec de synchronisation d'une source": { en: "Source synchronization failure", de: "Synchronisationsfehler einer Quelle", es: "Fallo de sincronización de una fuente", zh: "数据源同步失败", ar: "فشل مزامنة مصدر" },
        "📈 Digest conformité périodique": { en: "📈 Periodic compliance digest", de: "📈 Periodischer Compliance-Digest", es: "📈 Resumen periódico de cumplimiento", zh: "📈 定期合规摘要", ar: "📈 ملخص الامتثال الدوري" },
        "Activer le digest périodique": { en: "Enable periodic digest", de: "Periodischen Digest aktivieren", es: "Activar el resumen periódico", zh: "启用定期摘要", ar: "فعّل الملخص الدوري" },
        "Enregistrer les réglages": { en: "Save settings", de: "Einstellungen speichern", es: "Guardar ajustes", zh: "保存设置", ar: "احفظ الإعدادات" },
        "🛡 Double Authentification (MFA)": { en: "🛡 Two-Factor Authentication (MFA)", de: "🛡 Zwei-Faktor-Authentifizierung (MFA)", es: "🛡 Doble autenticación (MFA)", zh: "🛡 双因素认证（MFA）", ar: "🛡 المصادقة الثنائية (MFA)" },
        "Confirmer & activer": { en: "Confirm & enable", de: "Bestätigen & aktivieren", es: "Confirmar y activar", zh: "确认并启用", ar: "أكد وفعّل" },
        "🗄 Rétention des Données (Admin)": { en: "🗄 Data Retention (Admin)", de: "🗄 Datenaufbewahrung (Admin)", es: "🗄 Retención de datos (Admin)", zh: "🗄 数据保留（管理员）", ar: "🗄 الاحتفاظ بالبيانات (مدير)" },
        "Décisions de criblage": { en: "Screening decisions", de: "Screening-Entscheidungen", es: "Decisiones de cribado", zh: "筛查决定", ar: "قرارات الفحص" },
        "Alertes clôturées": { en: "Closed alerts", de: "Geschlossene Alarme", es: "Alertas cerradas", zh: "已关闭警报", ar: "تنبيهات مغلقة" },
        "Rapports de sync": { en: "Sync reports", de: "Sync-Berichte", es: "Informes de sync", zh: "同步报告", ar: "تقارير المزامنة" },
        "Campagnes batch": { en: "Batch campaigns", de: "Batch-Kampagnen", es: "Campañas por lotes", zh: "批量任务", ar: "حملات الدفعات" },
        "Heure de purge (cron 5 champs)": { en: "Purge time (5-field cron)", de: "Löschzeitpunkt (5-Feld-Cron)", es: "Hora de purga (cron 5 campos)", zh: "清除时间（5段cron）", ar: "وقت الحذف (cron من 5 حقول)" },
        "Enregistrer la politique": { en: "Save policy", de: "Richtlinie speichern", es: "Guardar política", zh: "保存策略", ar: "احفظ السياسة" },
        "🗑 Purger maintenant…": { en: "🗑 Purge now…", de: "🗑 Jetzt löschen…", es: "🗑 Purgar ahora…", zh: "🗑 立即清除…", ar: "🗑 احذف الآن…" },
        "🌴 Absence & Délégation": { en: "🌴 Absence & Delegation", de: "🌴 Abwesenheit & Delegation", es: "🌴 Ausencia y delegación", zh: "🌴 缺勤与委派", ar: "🌴 الغياب والتفويض" },
        "Absent jusqu'au": { en: "Absent until", de: "Abwesend bis", es: "Ausente hasta", zh: "缺勤至", ar: "غائب حتى" },
        "Délégué": { en: "Delegate", de: "Vertretung", es: "Delegado", zh: "代理人", ar: "المفوَّض" },
        "Choisir…": { en: "Choose…", de: "Wählen…", es: "Elegir…", zh: "选择…", ar: "اختر…" },
        "Réassigner immédiatement mes alertes ouvertes au délégué": { en: "Immediately reassign my open alerts to the delegate", de: "Meine offenen Alarme sofort der Vertretung zuweisen", es: "Reasignar de inmediato mis alertas abiertas al delegado", zh: "立即将我的未结警报转给代理人", ar: "أعد إسناد تنبيهاتي المفتوحة فورًا إلى المفوَّض" },
        "Déclarer l'absence": { en: "Declare absence", de: "Abwesenheit melden", es: "Declarar ausencia", zh: "登记缺勤", ar: "سجّل الغياب" },
        "Mettre fin à l'absence": { en: "End absence", de: "Abwesenheit beenden", es: "Finalizar ausencia", zh: "结束缺勤", ar: "أنهِ الغياب" },
        "🎯 Seuils de Score du Criblage (Admin)": { en: "🎯 Screening Score Thresholds (Admin)", de: "🎯 Screening-Schwellenwerte (Admin)", es: "🎯 Umbrales de puntuación del cribado (Admin)", zh: "🎯 筛查得分阈值（管理员）", ar: "🎯 عتبات درجات الفحص (مدير)" },
        "Seuil global (0-100)": { en: "Global threshold (0-100)", de: "Globaler Schwellenwert (0-100)", es: "Umbral global (0-100)", zh: "全局阈值（0-100）", ar: "العتبة العامة (0-100)" },
        "Enregistrer les seuils": { en: "Save thresholds", de: "Schwellenwerte speichern", es: "Guardar umbrales", zh: "保存阈值", ar: "احفظ العتبات" },
        "💼 Portabilité de la Configuration (Admin)": { en: "💼 Configuration Portability (Admin)", de: "💼 Konfigurations-Portabilität (Admin)", es: "💼 Portabilidad de la configuración (Admin)", zh: "💼 配置可移植性（管理员）", ar: "💼 نقل الإعدادات (مدير)" },
        "⬇ Exporter la configuration": { en: "⬇ Export configuration", de: "⬇ Konfiguration exportieren", es: "⬇ Exportar configuración", zh: "⬇ 导出配置", ar: "⬇ صدّر الإعدادات" },
        "⬆ Importer…": { en: "⬆ Import…", de: "⬆ Importieren…", es: "⬆ Importar…", zh: "⬆ 导入…", ar: "⬆ استورد…" },
        "🔑 Clés d'API Techniques (Admin)": { en: "🔑 Technical API Keys (Admin)", de: "🔑 Technische API-Schlüssel (Admin)", es: "🔑 Claves de API técnicas (Admin)", zh: "🔑 技术API密钥（管理员）", ar: "🔑 مفاتيح API تقنية (مدير)" },
        "➕ Créer la clé": { en: "➕ Create key", de: "➕ Schlüssel erstellen", es: "➕ Crear clave", zh: "➕ 创建密钥", ar: "➕ أنشئ المفتاح" },
        "Préfixe": { en: "Prefix", de: "Präfix", es: "Prefijo", zh: "前缀", ar: "البادئة" },
        "Rôles": { en: "Roles", de: "Rollen", es: "Roles", zh: "角色", ar: "الأدوار" },
        "Créée": { en: "Created", de: "Erstellt", es: "Creada", zh: "创建时间", ar: "أنشئت" },
        "Dernier usage": { en: "Last used", de: "Zuletzt verwendet", es: "Último uso", zh: "最近使用", ar: "آخر استخدام" },
        // Utilisateurs & modales
        "Gestion des Comptes & Droits d'Accès": { en: "Accounts & Access Rights", de: "Konten & Zugriffsrechte", es: "Cuentas y derechos de acceso", zh: "账户与访问权限", ar: "الحسابات وصلاحيات الوصول" },
        "➕ Nouveau Compte Utilisateur": { en: "➕ New user account", de: "➕ Neues Benutzerkonto", es: "➕ Nueva cuenta de usuario", zh: "➕ 新建用户", ar: "➕ حساب مستخدم جديد" },
        "Nom d'utilisateur": { en: "Username", de: "Benutzername", es: "Nombre de usuario", zh: "用户名", ar: "اسم المستخدم" },
        "Nom Complet": { en: "Full name", de: "Vollständiger Name", es: "Nombre completo", zh: "全名", ar: "الاسم الكامل" },
        "Rôle": { en: "Role", de: "Rolle", es: "Rol", zh: "角色", ar: "الدور" },
        "Date de Création": { en: "Creation date", de: "Erstellungsdatum", es: "Fecha de creación", zh: "创建日期", ar: "تاريخ الإنشاء" },
        "Créer un Utilisateur": { en: "Create user", de: "Benutzer anlegen", es: "Crear usuario", zh: "创建用户", ar: "إنشاء مستخدم" },
        "Rôle / Privilèges": { en: "Role / Privileges", de: "Rolle / Berechtigungen", es: "Rol / Privilegios", zh: "角色/权限", ar: "الدور / الامتيازات" },
        "Utilisateur (Criblage & Consultation)": { en: "User (screening & consultation)", de: "Benutzer (Screening & Einsicht)", es: "Usuario (cribado y consulta)", zh: "用户（筛查与查询）", ar: "مستخدم (فحص واطلاع)" },
        "Réviseur (Homologation des listes)": { en: "Reviewer (list approval)", de: "Prüfer (Listenfreigabe)", es: "Revisor (homologación de listas)", zh: "复核员（名单审批）", ar: "مراجع (اعتماد القوائم)" },
        "Utilisateur + Réviseur": { en: "User + Reviewer", de: "Benutzer + Prüfer", es: "Usuario + Revisor", zh: "用户+复核员", ar: "مستخدم + مراجع" },
        "Auditeur (Lecture seule intégrale)": { en: "Auditor (full read-only)", de: "Auditor (nur Lesen)", es: "Auditor (solo lectura)", zh: "审计员（完全只读）", ar: "مدقق (قراءة فقط)" },
        "Administrateur (Accès Complet)": { en: "Administrator (full access)", de: "Administrator (Vollzugriff)", es: "Administrador (acceso completo)", zh: "管理员（完全访问）", ar: "مدير (وصول كامل)" },
        "Mot de passe": { en: "Password", de: "Passwort", es: "Contraseña", zh: "密码", ar: "كلمة المرور" },
        "Annuler": { en: "Cancel", de: "Abbrechen", es: "Cancelar", zh: "取消", ar: "إلغاء" },
        "Fermer": { en: "Close", de: "Schließen", es: "Cerrar", zh: "关闭", ar: "إغلاق" },
        "Sauvegarder": { en: "Save", de: "Speichern", es: "Guardar", zh: "保存", ar: "حفظ" },
        "Confirmation": { en: "Confirmation", de: "Bestätigung", es: "Confirmación", zh: "确认", ar: "تأكيد" },
        "Confirmer": { en: "Confirm", de: "Bestätigen", es: "Confirmar", zh: "确认", ar: "تأكيد" },
        "Mon Profil & Sécurité": { en: "My profile & security", de: "Mein Profil & Sicherheit", es: "Mi perfil y seguridad", zh: "我的资料与安全", ar: "ملفي وأماني" },
        "Changer le Mot de Passe": { en: "Change password", de: "Passwort ändern", es: "Cambiar contraseña", zh: "修改密码", ar: "تغيير كلمة المرور" },
        "Mot de passe actuel": { en: "Current password", de: "Aktuelles Passwort", es: "Contraseña actual", zh: "当前密码", ar: "كلمة المرور الحالية" },
        "Nouveau mot de passe": { en: "New password", de: "Neues Passwort", es: "Nueva contraseña", zh: "新密码", ar: "كلمة المرور الجديدة" },
        "Mettre en Liste Blanche": { en: "Add to whitelist", de: "Auf die Whitelist setzen", es: "Añadir a lista blanca", zh: "加入白名单", ar: "أضف إلى القائمة البيضاء" },
        "Pièce jointe justificative": { en: "Supporting attachment", de: "Belegender Anhang", es: "Adjunto justificativo", zh: "证明附件", ar: "مرفق داعم" },
        "Confirmer la mise en liste blanche": { en: "Confirm whitelisting", de: "Whitelisting bestätigen", es: "Confirmar lista blanca", zh: "确认加入白名单", ar: "أكد الإدراج في القائمة البيضاء" },
        "Exclure de la Mise en Production": { en: "Exclude from production", de: "Von Produktivsetzung ausschließen", es: "Excluir de la puesta en producción", zh: "排除出投产", ar: "استبعد من النشر" },
        "Confirmer l'exclusion": { en: "Confirm exclusion", de: "Ausschluss bestätigen", es: "Confirmar exclusión", zh: "确认排除", ar: "أكد الاستبعاد" },
        "Détail de la Décision (Piste d'Audit)": { en: "Decision detail (audit trail)", de: "Entscheidungsdetail (Audit-Trail)", es: "Detalle de la decisión (pista de auditoría)", zh: "决定详情（审计轨迹）", ar: "تفاصيل القرار (سجل التدقيق)" },
        "Détails de l'Entité": { en: "Entity details", de: "Entitätsdetails", es: "Detalles de la entidad", zh: "实体详情", ar: "تفاصيل الكيان" },
        "👤 Vue client 360°": { en: "👤 Client 360° view", de: "👤 Kunden-360°-Sicht", es: "👤 Vista cliente 360°", zh: "👤 客户360°视图", ar: "👤 عرض العميل 360°" },
        "🕸 Graphe des relations": { en: "🕸 Relationship graph", de: "🕸 Beziehungsgraph", es: "🕸 Grafo de relaciones", zh: "🕸 关系图谱", ar: "🕸 مخطط العلاقات" },
        "Profondeur": { en: "Depth", de: "Tiefe", es: "Profundidad", zh: "深度", ar: "العمق" },
        "Détention majoritaire (règle des 50 %)": { en: "Majority ownership (50% rule)", de: "Mehrheitsbeteiligung (50-%-Regel)", es: "Participación mayoritaria (regla del 50 %)", zh: "多数持股（50%规则）", ar: "ملكية أغلبية (قاعدة 50٪)" },
        "Autre relation": { en: "Other relationship", de: "Andere Beziehung", es: "Otra relación", zh: "其他关系", ar: "علاقة أخرى" },
        "Personne physique": { en: "Natural person", de: "Natürliche Person", es: "Persona física", zh: "自然人", ar: "شخص طبيعي" },
        "Personne morale / autre": { en: "Legal person / other", de: "Juristische Person / andere", es: "Persona jurídica / otra", zh: "法人/其他", ar: "شخص اعتباري / آخر" },
        // Attributs (title / placeholder / aria-label)
        "Basculer le thème clair/sombre": { en: "Toggle light/dark theme", de: "Hell-/Dunkelmodus umschalten", es: "Cambiar tema claro/oscuro", zh: "切换明暗主题", ar: "بدّل السمة الفاتحة/الداكنة" },
        "Centre de notifications": { en: "Notification center", de: "Benachrichtigungszentrum", es: "Centro de notificaciones", zh: "通知中心", ar: "مركز الإشعارات" },
        "Notifications": { en: "Notifications", de: "Benachrichtigungen", es: "Notificaciones", zh: "通知", ar: "الإشعارات" },
        "Recherche globale": { en: "Global search", de: "Globale Suche", es: "Búsqueda global", zh: "全局搜索", ar: "بحث شامل" },
        "Recherche globale (Ctrl+K)": { en: "Global search (Ctrl+K)", de: "Globale Suche (Strg+K)", es: "Búsqueda global (Ctrl+K)", zh: "全局搜索（Ctrl+K）", ar: "بحث شامل (Ctrl+K)" },
        "Ouvrir le menu de navigation": { en: "Open navigation menu", de: "Navigationsmenü öffnen", es: "Abrir menú de navegación", zh: "打开导航菜单", ar: "افتح قائمة التنقل" },
        "Se déconnecter de la session": { en: "Log out of the session", de: "Sitzung abmelden", es: "Cerrar la sesión", zh: "退出会话", ar: "تسجيل الخروج من الجلسة" },
        "Gérer mon profil et mot de passe": { en: "Manage my profile and password", de: "Profil und Passwort verwalten", es: "Gestionar mi perfil y contraseña", zh: "管理我的资料与密码", ar: "إدارة ملفي وكلمة مروري" },
        "Thème clair / sombre": { en: "Light / dark theme", de: "Helles / dunkles Design", es: "Tema claro / oscuro", zh: "明/暗主题", ar: "سمة فاتحة / داكنة" },
        "Tout sélectionner": { en: "Select all", de: "Alle auswählen", es: "Seleccionar todo", zh: "全选", ar: "تحديد الكل" },
        "Vues sauvegardées": { en: "Saved views", de: "Gespeicherte Ansichten", es: "Vistas guardadas", zh: "已存视图", ar: "طرق عرض محفوظة" },
        "Sauvegarder les filtres courants comme vue": { en: "Save current filters as a view", de: "Aktuelle Filter als Ansicht speichern", es: "Guardar filtros actuales como vista", zh: "将当前筛选存为视图", ar: "احفظ عوامل التصفية كطريقة عرض" },
        "Filtrer par priorité": { en: "Filter by priority", de: "Nach Priorität filtern", es: "Filtrar por prioridad", zh: "按优先级筛选", ar: "صفِّ حسب الأولوية" },
        "Filtrer par type de liste": { en: "Filter by list type", de: "Nach Listentyp filtern", es: "Filtrar por tipo de lista", zh: "按名单类型筛选", ar: "صفِّ حسب نوع القائمة" },
        "Filtrer par décision": { en: "Filter by decision", de: "Nach Entscheidung filtern", es: "Filtrar por decisión", zh: "按决定筛选", ar: "صفِّ حسب القرار" },
        "Filtrer par statut en base": { en: "Filter by database status", de: "Nach DB-Status filtern", es: "Filtrar por estado en base", zh: "按库内状态筛选", ar: "صفِّ حسب حالة القاعدة" },
        "Filtrer par nom ou ID...": { en: "Filter by name or ID...", de: "Nach Name oder ID filtern...", es: "Filtrar por nombre o ID...", zh: "按姓名或ID筛选…", ar: "صفِّ بالاسم أو المعرف..." },
        "Exporter la file filtrée en CSV": { en: "Export filtered queue to CSV", de: "Gefilterte Liste als CSV exportieren", es: "Exportar la cola filtrada a CSV", zh: "导出筛选后的队列为CSV", ar: "صدّر القائمة المصفاة إلى CSV" },
        "Exporter la vue filtrée en CSV": { en: "Export filtered view to CSV", de: "Gefilterte Ansicht als CSV exportieren", es: "Exportar la vista filtrada a CSV", zh: "导出筛选视图为CSV", ar: "صدّر العرض المصفى إلى CSV" },
        "Exporter le journal filtré en CSV": { en: "Export filtered log to CSV", de: "Gefiltertes Protokoll als CSV exportieren", es: "Exportar el registro filtrado a CSV", zh: "导出筛选日志为CSV", ar: "صدّر السجل المصفى إلى CSV" },
        "Export CSV Excel": { en: "Excel CSV export", de: "Excel-CSV-Export", es: "Exportación CSV Excel", zh: "Excel CSV 导出", ar: "تصدير CSV لإكسل" },
        "Rapport imprimable": { en: "Printable report", de: "Druckbarer Bericht", es: "Informe imprimible", zh: "可打印报告", ar: "تقرير قابل للطباعة" },
        "Champ dans lequel rechercher": { en: "Field to search in", de: "Suchfeld", es: "Campo de búsqueda", zh: "搜索字段", ar: "حقل البحث" },
        "🔍 Rechercher (nom, ID, LEI, IMO)...": { en: "🔍 Search (name, ID, LEI, IMO)...", de: "🔍 Suchen (Name, ID, LEI, IMO)...", es: "🔍 Buscar (nombre, ID, LEI, IMO)...", zh: "🔍 搜索（姓名、ID、LEI、IMO）…", ar: "🔍 ابحث (اسم، معرف، LEI، IMO)..." },
        // Libellés dynamiques (rendus par app.js)
        "OUVERTE": { en: "OPEN", de: "OFFEN", es: "ABIERTA", zh: "未处理", ar: "مفتوحة" },
        "EN COURS": { en: "IN PROGRESS", de: "IN BEARBEITUNG", es: "EN CURSO", zh: "处理中", ar: "قيد المعالجة" },
        "ESCALADÉE": { en: "ESCALATED", de: "ESKALIERT", es: "ESCALADA", zh: "已升级", ar: "مصعّدة" },
        "À VALIDER (4-YEUX)": { en: "TO VALIDATE (4-EYES)", de: "ZU PRÜFEN (4-AUGEN)", es: "POR VALIDAR (4 OJOS)", zh: "待复核（四眼）", ar: "بانتظار التحقق (أربع أعين)" },
        "VRAI POSITIF": { en: "TRUE POSITIVE", de: "ECHTER TREFFER", es: "VERDADERO POSITIVO", zh: "真阳性", ar: "إيجابي حقيقي" },
        "FAUX POSITIF": { en: "FALSE POSITIVE", de: "FEHLALARM", es: "FALSO POSITIVO", zh: "误报", ar: "إيجابي كاذب" },
        "CLÔTURÉE PAR RÈGLE": { en: "CLOSED BY RULE", de: "PER REGEL GESCHLOSSEN", es: "CERRADA POR REGLA", zh: "按规则关闭", ar: "أُغلقت بقاعدة" },
        "CRITIQUE": { en: "CRITICAL", de: "KRITISCH", es: "CRÍTICA", zh: "严重", ar: "حرجة" },
        "HAUTE": { en: "HIGH", de: "HOCH", es: "ALTA", zh: "高", ar: "مرتفعة" },
        "MOYENNE": { en: "MEDIUM", de: "MITTEL", es: "MEDIA", zh: "中", ar: "متوسطة" },
        "BASSE": { en: "LOW", de: "NIEDRIG", es: "BAJA", zh: "低", ar: "منخفضة" },
        "⏰ EN RETARD": { en: "⏰ OVERDUE", de: "⏰ ÜBERFÄLLIG", es: "⏰ ATRASADA", zh: "⏰ 逾期", ar: "⏰ متأخرة" },
        "🔎 Instruire": { en: "🔎 Investigate", de: "🔎 Bearbeiten", es: "🔎 Instruir", zh: "🔎 调查", ar: "🔎 تحقيق" },
        "👤 Client 360°": { en: "👤 Client 360°", de: "👤 Kunde 360°", es: "👤 Cliente 360°", zh: "👤 客户360°", ar: "👤 عميل 360°" },
        "🖨 Rapport": { en: "🖨 Report", de: "🖨 Bericht", es: "🖨 Informe", zh: "🖨 报告", ar: "🖨 تقرير" },
        "✏️ Éditer": { en: "✏️ Edit", de: "✏️ Bearbeiten", es: "✏️ Editar", zh: "✏️ 编辑", ar: "✏️ تحرير" },
        "🗑️ Supprimer": { en: "🗑️ Delete", de: "🗑️ Löschen", es: "🗑️ Eliminar", zh: "🗑️ 删除", ar: "🗑️ حذف" },
        "VOUS": { en: "YOU", de: "SIE", es: "USTED", zh: "本人", ar: "أنت" },
        "ADMINISTRATEUR": { en: "ADMINISTRATOR", de: "ADMINISTRATOR", es: "ADMINISTRADOR", zh: "管理员", ar: "مدير" },
        "RÉVISEUR": { en: "REVIEWER", de: "PRÜFER", es: "REVISOR", zh: "复核员", ar: "مراجع" },
        "ANALYSTE USER": { en: "USER ANALYST", de: "ANALYST (USER)", es: "ANALISTA USER", zh: "分析员", ar: "محلل" },
        "AUDITEUR (LECTURE SEULE)": { en: "AUDITOR (READ-ONLY)", de: "AUDITOR (NUR LESEN)", es: "AUDITOR (SOLO LECTURA)", zh: "审计员（只读）", ar: "مدقق (قراءة فقط)" },
        "📌 M'assigner": { en: "📌 Assign to me", de: "📌 Mir zuweisen", es: "📌 Asignarme", zh: "📌 指派给我", ar: "📌 أسند إلي" },
        "💬 Commenter": { en: "💬 Comment", de: "💬 Kommentieren", es: "💬 Comentar", zh: "💬 评论", ar: "💬 تعليق" },
        "⚠️ Escalader": { en: "⚠️ Escalate", de: "⚠️ Eskalieren", es: "⚠️ Escalar", zh: "⚠️ 升级", ar: "⚠️ تصعيد" },
        "✅ Proposer : Faux positif": { en: "✅ Propose: false positive", de: "✅ Vorschlagen: Fehlalarm", es: "✅ Proponer: falso positivo", zh: "✅ 提议：误报", ar: "✅ اقترح: إيجابي كاذب" },
        "🚨 Proposer : Vrai positif": { en: "🚨 Propose: true positive", de: "🚨 Vorschlagen: echter Treffer", es: "🚨 Proponer: verdadero positivo", zh: "🚨 提议：真阳性", ar: "🚨 اقترح: إيجابي حقيقي" },
        "✔️ Valider (4-yeux)": { en: "✔️ Validate (4-eyes)", de: "✔️ Freigeben (4-Augen)", es: "✔️ Validar (4 ojos)", zh: "✔️ 复核通过（四眼）", ar: "✔️ تحقق (أربع أعين)" },
        "↩️ Refuser & renvoyer": { en: "↩️ Refuse & send back", de: "↩️ Ablehnen & zurücksenden", es: "↩️ Rechazar y devolver", zh: "↩️ 拒绝并退回", ar: "↩️ ارفض وأعد" },
        "← Précédent": { en: "← Previous", de: "← Zurück", es: "← Anterior", zh: "← 上一页", ar: "← السابق" },
        "Suivant →": { en: "Next →", de: "Weiter →", es: "Siguiente →", zh: "下一页 →", ar: "التالي →" },
        "Aucune alerte pour ce filtre.": { en: "No alert for this filter.", de: "Kein Alarm für diesen Filter.", es: "Ninguna alerta para este filtro.", zh: "此筛选无警报。", ar: "لا تنبيهات لهذا الفلتر." },
        "Aucun utilisateur trouvé.": { en: "No user found.", de: "Kein Benutzer gefunden.", es: "Ningún usuario encontrado.", zh: "未找到用户。", ar: "لا يوجد مستخدم." },
        "Aucune paire.": { en: "No pair.", de: "Kein Paar.", es: "Ningún par.", zh: "无配对。", ar: "لا أزواج." },
        "Aucune alerte ouverte : file à jour.": { en: "No open alert: queue is clear.", de: "Kein offener Alarm: Liste ist leer.", es: "Sin alertas abiertas: cola al día.", zh: "无未结警报：队列已清。", ar: "لا تنبيهات مفتوحة: القائمة فارغة." },
        "Rien à purger avec la politique actuelle.": { en: "Nothing to purge with the current policy.", de: "Mit aktueller Richtlinie nichts zu löschen.", es: "Nada que purgar con la política actual.", zh: "按当前策略无可清除项。", ar: "لا شيء للحذف وفق السياسة الحالية." },
        // Page de connexion
        "Plateforme de Conformité & Criblage Sanctions": { en: "Compliance & Sanctions Screening Platform", de: "Compliance- & Sanktions-Screening-Plattform", es: "Plataforma de cumplimiento y cribado de sanciones", zh: "合规与制裁筛查平台", ar: "منصة الامتثال وفحص العقوبات" },
        "Se Connecter": { en: "Log in", de: "Anmelden", es: "Iniciar sesión", zh: "登录", ar: "تسجيل الدخول" },
        "Connexion en cours...": { en: "Logging in...", de: "Anmeldung läuft...", es: "Iniciando sesión...", zh: "正在登录…", ar: "جارٍ تسجيل الدخول..." },
        "Identifiants invalides": { en: "Invalid credentials", de: "Ungültige Anmeldedaten", es: "Credenciales no válidas", zh: "凭据无效", ar: "بيانات اعتماد غير صالحة" },
        "Code de vérification (MFA)": { en: "Verification code (MFA)", de: "Bestätigungscode (MFA)", es: "Código de verificación (MFA)", zh: "验证码（MFA）", ar: "رمز التحقق (MFA)" },
        "Fiskr Security Engine • Identification requise (ACPR / AMF)": { en: "Fiskr Security Engine • Identification required (ACPR / AMF)", de: "Fiskr Security Engine • Identifikation erforderlich (ACPR / AMF)", es: "Fiskr Security Engine • Identificación requerida (ACPR / AMF)", zh: "Fiskr 安全引擎 • 需要身份验证（ACPR / AMF）", ar: "محرك أمان Fiskr • مطلوب تحديد الهوية (ACPR / AMF)" },
        // Libelles de statuts (STATUS_LABELS, rendus dynamiques)
        "Ouverte": { en: "Open", de: "Offen", es: "Abierta", zh: "未处理", ar: "مفتوحة" },
        "En cours": { en: "In progress", de: "In Bearbeitung", es: "En curso", zh: "处理中", ar: "قيد المعالجة" },
        "Escaladée": { en: "Escalated", de: "Eskaliert", es: "Escalada", zh: "已升级", ar: "مصعّدة" },
        "À valider (4 yeux)": { en: "To validate (4-eyes)", de: "Zu prüfen (4-Augen)", es: "Por validar (4 ojos)", zh: "待复核（四眼）", ar: "بانتظار التحقق" },
        "Vrai positif": { en: "True positive", de: "Echter Treffer", es: "Verdadero positivo", zh: "真阳性", ar: "إيجابي حقيقي" },
        "Faux positif": { en: "False positive", de: "Fehlalarm", es: "Falso positivo", zh: "误报", ar: "إيجابي كاذب" },
        "Close par règle": { en: "Closed by rule", de: "Per Regel geschlossen", es: "Cerrada por regla", zh: "按规则关闭", ar: "أُغلقت بقاعدة" },
        "En homologation": { en: "Pending approval", de: "In Freigabe", es: "En homologación", zh: "审批中", ar: "قيد الاعتماد" },
        "Remplacé": { en: "Superseded", de: "Ersetzt", es: "Sustituido", zh: "已替换", ar: "مستبدل" },
        "Rejeté": { en: "Rejected", de: "Abgelehnt", es: "Rechazado", zh: "已拒绝", ar: "مرفوض" },
        "En traitement": { en: "Processing", de: "In Verarbeitung", es: "En tratamiento", zh: "处理中", ar: "قيد المعالجة" },
        "Erreur": { en: "Error", de: "Fehler", es: "Error", zh: "错误", ar: "خطأ" },
        "Brouillon": { en: "Draft", de: "Entwurf", es: "Borrador", zh: "草稿", ar: "مسودة" },
        "Active": { en: "Active", de: "Aktiv", es: "Activa", zh: "启用", ar: "نشطة" },
        "Ajouté": { en: "Added", de: "Hinzugefügt", es: "Añadido", zh: "已新增", ar: "مضاف" },
        "Supprimé": { en: "Removed", de: "Entfernt", es: "Eliminado", zh: "已删除", ar: "محذوف" },
        "Modifié": { en: "Modified", de: "Geändert", es: "Modificado", zh: "已修改", ar: "معدل" },
        "Alerte": { en: "Alert", de: "Alarm", es: "Alerta", zh: "警报", ar: "تنبيه" },
        // Etats vides et messages frequents (rendus dynamiques)
        "Présent": { en: "Present", de: "Anwesend", es: "Presente", zh: "在岗", ar: "حاضر" },
        "— aucune délégation active.": { en: "— no active delegation.", de: "— keine aktive Delegation.", es: "— sin delegación activa.", zh: "— 无有效委派。", ar: "— لا تفويض نشطاً." },
        "🌴 Absence active": { en: "🌴 Absence active", de: "🌴 Abwesenheit aktiv", es: "🌴 Ausencia activa", zh: "🌴 缺勤中", ar: "🌴 غياب نشط" },
        "🛡 MFA active": { en: "🛡 MFA enabled", de: "🛡 MFA aktiv", es: "🛡 MFA activa", zh: "🛡 MFA 已启用", ar: "🛡 MFA مفعلة" },
        "MFA inactive": { en: "MFA disabled", de: "MFA inaktiv", es: "MFA inactiva", zh: "MFA 未启用", ar: "MFA معطلة" },
        "Activer la MFA…": { en: "Enable MFA…", de: "MFA aktivieren…", es: "Activar MFA…", zh: "启用 MFA…", ar: "فعّل MFA…" },
        "Désactiver la MFA…": { en: "Disable MFA…", de: "MFA deaktivieren…", es: "Desactivar MFA…", zh: "停用 MFA…", ar: "عطّل MFA…" },
        // Progression des operations longues (imports, synchronisations)
        "Préparation…": { en: "Preparing…", de: "Vorbereitung…", es: "Preparando…", zh: "准备中…", ar: "جارٍ التحضير…" },
        "Téléversement du fichier…": { en: "Uploading file…", de: "Datei wird hochgeladen…", es: "Subiendo el archivo…", zh: "正在上传文件…", ar: "جارٍ رفع الملف…" },
        "Téléchargement depuis la source…": { en: "Downloading from source…", de: "Download von der Quelle…", es: "Descargando desde la fuente…", zh: "正在从数据源下载…", ar: "جارٍ التنزيل من المصدر…" },
        "Calcul de l'empreinte SHA-256…": { en: "Computing SHA-256 hash…", de: "SHA-256-Prüfsumme wird berechnet…", es: "Calculando la huella SHA-256…", zh: "正在计算 SHA-256 哈希…", ar: "جارٍ حساب بصمة SHA-256…" },
        "Analyse du fichier…": { en: "Parsing file…", de: "Datei wird analysiert…", es: "Analizando el archivo…", zh: "正在解析文件…", ar: "جارٍ تحليل الملف…" },
        "Enregistrement des fiches…": { en: "Saving records…", de: "Einträge werden gespeichert…", es: "Guardando las fichas…", zh: "正在保存记录…", ar: "جارٍ حفظ السجلات…" },
        "Calcul du delta…": { en: "Computing delta…", de: "Delta wird berechnet…", es: "Calculando el delta…", zh: "正在计算增量…", ar: "جارٍ حساب الفروق…" },
        "Rechargement du cache de production…": { en: "Reloading production cache…", de: "Produktions-Cache wird neu geladen…", es: "Recargando la caché de producción…", zh: "正在重载生产缓存…", ar: "جارٍ إعادة تحميل ذاكرة الإنتاج…" },
        "Terminé": { en: "Done", de: "Fertig", es: "Terminado", zh: "已完成", ar: "منتهٍ" },
        // Atelier de regles anti-FP (aides a l'edition + langage naturel)
        "🗣️ Décrire la règle en langage naturel": { en: "🗣️ Describe the rule in natural language", de: "🗣️ Regel in natürlicher Sprache beschreiben", es: "🗣️ Describir la regla en lenguaje natural", zh: "🗣️ 用自然语言描述规则", ar: "🗣️ صف القاعدة بلغة طبيعية" },
        "✨ Générer par IA": { en: "✨ Generate with AI", de: "✨ Mit KI generieren", es: "✨ Generar con IA", zh: "✨ AI 生成", ar: "✨ توليد بالذكاء الاصطناعي" },
        "🧩 Formulaire structuré (sans IA)": { en: "🧩 Structured form (no AI)", de: "🧩 Strukturiertes Formular (ohne KI)", es: "🧩 Formulario estructurado (sin IA)", zh: "🧩 结构化表单（无 AI）", ar: "🧩 نموذج منظم (بدون ذكاء اصطناعي)" },
        "✓ Vérifier la syntaxe": { en: "✓ Check syntax", de: "✓ Syntax prüfen", es: "✓ Verificar la sintaxis", zh: "✓ 检查语法", ar: "✓ تحقق من الصياغة" },
        "📋 Insérer un modèle…": { en: "📋 Insert a template…", de: "📋 Vorlage einfügen…", es: "📋 Insertar un modelo…", zh: "📋 插入模板…", ar: "📋 أدرج نموذجًا…" },
        "🎯 + Test depuis une alerte": { en: "🎯 + Test from an alert", de: "🎯 + Test aus einem Alarm", es: "🎯 + Prueba desde una alerta", zh: "🎯 + 从警报创建测试", ar: "🎯 + اختبار من تنبيه" },
        "+ Condition": { en: "+ Condition", de: "+ Bedingung", es: "+ Condición", zh: "+ 条件", ar: "+ شرط" },
        "⤵ Générer le code dans l'éditeur": { en: "⤵ Generate code in the editor", de: "⤵ Code im Editor erzeugen", es: "⤵ Generar el código en el editor", zh: "⤵ 在编辑器中生成代码", ar: "⤵ ولّد الكود في المحرر" },
        "Ne jamais supprimer un hard match": { en: "Never suppress a hard match", de: "Hard Match nie unterdrücken", es: "Nunca suprimir un hard match", zh: "绝不抑制硬匹配", ar: "لا تحذف تطابقًا صارمًا أبدًا" },
        "Combinaison": { en: "Combination", de: "Verknüpfung", es: "Combinación", zh: "组合方式", ar: "الدمج" },
        "ET (toutes)": { en: "AND (all)", de: "UND (alle)", es: "Y (todas)", zh: "且（全部满足）", ar: "و (كلها)" },
        "OU (au moins une)": { en: "OR (at least one)", de: "ODER (mindestens eine)", es: "O (al menos una)", zh: "或（至少一项）", ar: "أو (واحد على الأقل)" },
        // Cahier de tests : regle candidate
        "Règle anti-FP candidate à évaluer (facultatif)": { en: "Candidate anti-FP rule to evaluate (optional)", de: "Zu bewertende Anti-FP-Kandidatenregel (optional)", es: "Regla anti-FP candidata a evaluar (opcional)", zh: "待评估的候选反误报规则（可选）", ar: "قاعدة مكافحة الإنذارات الكاذبة المرشحة للتقييم (اختياري)" },
        "Aucune — règles actives uniquement": { en: "None — active rules only", de: "Keine — nur aktive Regeln", es: "Ninguna — solo reglas activas", zh: "无 — 仅使用生效规则", ar: "بدون — القواعد النشطة فقط" },
        "🧩 Règles anti-faux positifs dans ce cahier de tests": { en: "🧩 Anti-false-positive rules in this test book", de: "🧩 Anti-Fehlalarm-Regeln in diesem Testheft", es: "🧩 Reglas antifalsos positivos en este cuaderno de pruebas", zh: "🧩 本测试用例集中的反误报规则", ar: "🧩 قواعد مكافحة الإنذارات الكاذبة في دفتر الاختبارات هذا" },
        "✏️ Créer une règle (onglet Alertes → Paramétrage & Règles)": { en: "✏️ Create a rule (Alerts tab → Settings & Rules)", de: "✏️ Regel anlegen (Tab Alarme → Parametrierung & Regeln)", es: "✏️ Crear una regla (pestaña Alertas → Parámetros y reglas)", zh: "✏️ 创建规则（警报页签 → 参数与规则）", ar: "✏️ أنشئ قاعدة (تبويب التنبيهات ← الإعدادات والقواعد)" },
        // Conformite+ : projet TRACFIN + qualite des donnees clients
        "🇫🇷 Projet de déclaration": { en: "🇫🇷 Draft suspicious activity report", de: "🇫🇷 Entwurf Verdachtsmeldung", es: "🇫🇷 Borrador de declaración", zh: "🇫🇷 可疑申报草稿", ar: "🇫🇷 مسودة بلاغ الاشتباه" },
        "🧪 Qualité des Données Clients": { en: "🧪 Client Data Quality", de: "🧪 Kundendatenqualität", es: "🧪 Calidad de los Datos de Clientes", zh: "🧪 客户数据质量", ar: "🧪 جودة بيانات العملاء" },
        "Segments les moins complets": { en: "Least complete segments", de: "Unvollständigste Segmente", es: "Segmentos menos completos", zh: "完整度最低的客群", ar: "الشرائح الأقل اكتمالًا" },
        "Fiches à risque pour le criblage": { en: "Records at risk for screening", de: "Risikoakten fürs Screening", es: "Fichas de riesgo para el cribado", zh: "对筛查有风险的档案", ar: "سجلات تشكل خطراً على الفحص" },
        "Segment": { en: "Segment", de: "Segment", es: "Segmento", zh: "客群", ar: "الشريحة" },
        "Clients": { en: "Clients", de: "Kunden", es: "Clientes", zh: "客户数", ar: "العملاء" },
        "Complétude": { en: "Completeness", de: "Vollständigkeit", es: "Completitud", zh: "完整度", ar: "الاكتمال" },
    };

    // ---- Paragraphes descriptifs (section-desc), cles = texte francais normalise ----
    const P = {
        "Consultation en direct de la base relationnelle : chaque affichage interroge la base (recherche, filtres et pagination côté serveur). Le périmètre « En production » reflète les instantanés homologués criblés par le moteur ; les autres statuts (en attente, remplacées, rejetées, exclues) sont consultables via le filtre Statut.": {"en": "Live view of the relational database: every display queries the database (server-side search, filters and pagination). The “In production” scope reflects the approved snapshots screened by the engine; the other statuses (pending, superseded, rejected, excluded) are available through the Status filter.", "de": "Live-Ansicht der relationalen Datenbank: Jede Anzeige fragt die Datenbank ab (serverseitige Suche, Filter und Paginierung). Der Bereich „Produktiv“ zeigt die freigegebenen, vom Engine gescreenten Snapshots; die anderen Status (ausstehend, ersetzt, abgelehnt, ausgeschlossen) sind über den Statusfilter abrufbar.", "es": "Consulta en vivo de la base relacional: cada pantalla interroga la base (búsqueda, filtros y paginación en el servidor). El perímetro «En producción» refleja las instantáneas homologadas cribadas por el motor; los demás estados (pendientes, sustituidas, rechazadas, excluidas) se consultan con el filtro Estado.", "zh": "实时查询关系型数据库：每次显示都直接查库（服务器端搜索、筛选与分页）。“在产”范围对应引擎实际筛查的已审批快照；其他状态（待审、已替换、已拒绝、已排除）可通过状态筛选器查看。", "ar": "عرض مباشر لقاعدة البيانات العلائقية: كل عرض يستعلم من القاعدة (بحث وتصفية وترقيم من جهة الخادم). نطاق «قيد الإنتاج» يعكس اللقطات المعتمدة التي يفحصها المحرك؛ الحالات الأخرى (معلقة، مستبدلة، مرفوضة، مستبعدة) متاحة عبر مرشح الحالة."},
        "Téléversez un fichier de watchlist ou de clients dans la base de données relationnelle. Le fichier importé apparaît ensuite dans l'onglet « Snapshots & Comparateur » (ou en Homologation si le pointage humain est actif).": {"en": "Upload a watchlist or client file into the relational database. The imported file then appears in the “Snapshots & Comparator” tab (or in Approval when human sign-off is enabled).", "de": "Laden Sie eine Watchlist- oder Kundendatei in die relationale Datenbank hoch. Die importierte Datei erscheint anschließend im Tab „Snapshots & Vergleich“ (oder in der Freigabe, wenn die menschliche Abnahme aktiv ist).", "es": "Suba un archivo de lista o de clientes a la base de datos relacional. El archivo importado aparece luego en la pestaña «Instantáneas y comparador» (o en Homologación si la validación humana está activa).", "zh": "将监控名单或客户文件上传到关系型数据库。导入的文件随后出现在“快照与比对”页签（若启用人工复核则进入审批）。", "ar": "حمّل ملف قائمة مراقبة أو عملاء إلى قاعدة البيانات. يظهر الملف المستورد بعدها في تبويب «اللقطات والمقارنة» (أو في الاعتماد إذا كان التحقق البشري مفعلاً)."},
        "Pipeline SSIE en 3 phases (Découverte → Résolution → Restitution) : les balises pivots sont configurables pour supporter tout flux XML référencé par ID (OFAC Advanced, SWIFT SLD, etc.).": {"en": "3-phase SSIE pipeline (Discovery → Resolution → Restitution): the pivot tags are configurable to support any ID-referenced XML feed (OFAC Advanced, SWIFT SLD, etc.).", "de": "3-Phasen-SSIE-Pipeline (Discovery → Resolution → Restitution): Die Pivot-Tags sind konfigurierbar und unterstützen jeden ID-referenzierten XML-Feed (OFAC Advanced, SWIFT SLD usw.).", "es": "Pipeline SSIE en 3 fases (Descubrimiento → Resolución → Restitución): las etiquetas pivote son configurables para soportar cualquier flujo XML referenciado por ID (OFAC Advanced, SWIFT SLD, etc.).", "zh": "SSIE 三阶段流水线（发现 → 解析 → 还原）：枢纽标签可配置，支持任何按 ID 引用的 XML 数据流（OFAC Advanced、SWIFT SLD 等）。", "ar": "خط أنابيب SSIE بثلاث مراحل (اكتشاف ← حل ← إرجاع): الوسوم المحورية قابلة للتكوين لدعم أي تدفق XML مُرجع بالمعرفات (OFAC Advanced، SWIFT SLD، إلخ)."},
        "Identifiez les modifications structurelles (ADDED, REMOVED, MODIFIED) entre deux versions de snapshots.": {"en": "Identify the structural changes (ADDED, REMOVED, MODIFIED) between two snapshot versions.", "de": "Ermitteln Sie die strukturellen Änderungen (ADDED, REMOVED, MODIFIED) zwischen zwei Snapshot-Versionen.", "es": "Identifique los cambios estructurales (ADDED, REMOVED, MODIFIED) entre dos versiones de instantáneas.", "zh": "识别两个快照版本之间的结构变更（ADDED、REMOVED、MODIFIED）。", "ar": "حدد التغييرات الهيكلية (ADDED، REMOVED، MODIFIED) بين نسختين من اللقطات."},
        "Liste de tous les instantanés et versions importés dans la base de données relationnelle.": {"en": "List of all snapshots and versions imported into the relational database.", "de": "Liste aller in die relationale Datenbank importierten Snapshots und Versionen.", "es": "Lista de todas las instantáneas y versiones importadas en la base de datos relacional.", "zh": "导入关系型数据库的全部快照与版本列表。", "ar": "قائمة بجميع اللقطات والنسخ المستوردة إلى قاعدة البيانات."},
        "Saisissez les informations de l'entité. Elle sera soumise au Data Quality Gate et indexée immédiatement dans le moteur de screening.": {"en": "Enter the entity's details. It will go through the Data Quality Gate and be indexed immediately in the screening engine.", "de": "Erfassen Sie die Daten der Entität. Sie durchläuft das Data Quality Gate und wird sofort im Screening-Engine indexiert.", "es": "Introduzca los datos de la entidad. Pasará por el Data Quality Gate y se indexará de inmediato en el motor de cribado.", "zh": "录入实体信息。它将通过数据质量门控并立即在筛查引擎中建立索引。", "ar": "أدخل بيانات الكيان. ستمر عبر بوابة جودة البيانات وتُفهرس فوراً في محرك الفحص."},
        "Récupérez les listes directement auprès des émetteurs : le delta (ADDED / MODIFIED / REMOVED) est calculé puis appliqué au référentiel actif, et un rapport de suivi est émis (in-app + email si SMTP configuré).": {"en": "Fetch the lists directly from the issuers: the delta (ADDED / MODIFIED / REMOVED) is computed then applied to the active referential, and a follow-up report is issued (in-app + email if SMTP is configured).", "de": "Beziehen Sie die Listen direkt von den Herausgebern: Das Delta (ADDED / MODIFIED / REMOVED) wird berechnet, auf das aktive Referenzial angewendet und ein Bericht erstellt (in-App + E-Mail bei konfiguriertem SMTP).", "es": "Recupere las listas directamente de los emisores: el delta (ADDED / MODIFIED / REMOVED) se calcula y se aplica al referencial activo, y se emite un informe de seguimiento (in-app + correo si SMTP está configurado).", "zh": "直接从发布方获取名单：计算增量（ADDED / MODIFIED / REMOVED）并应用到当前参照库，同时生成跟踪报告（应用内 + 邮件，若已配置 SMTP）。", "ar": "اجلب القوائم مباشرة من جهات الإصدار: يُحسب الفرق (ADDED / MODIFIED / REMOVED) ثم يُطبق على المرجع النشط، ويصدر تقرير متابعة (داخل التطبيق + بريد إذا كان SMTP مكوناً)."},
        "Téléchargement du fichier officiel SDN_ADVANCED.XML, ingestion, delta et remplacement de la liste OFAC active.": {"en": "Download of the official SDN_ADVANCED.XML file, ingestion, delta and replacement of the active OFAC list.", "de": "Download der offiziellen Datei SDN_ADVANCED.XML, Ingestion, Delta und Ersetzung der aktiven OFAC-Liste.", "es": "Descarga del archivo oficial SDN_ADVANCED.XML, ingestión, delta y sustitución de la lista OFAC activa.", "zh": "下载官方 SDN_ADVANCED.XML 文件，入库、计算增量并替换当前 OFAC 名单。", "ar": "تنزيل الملف الرسمي SDN_ADVANCED.XML وإدخاله وحساب الفرق واستبدال قائمة OFAC النشطة."},
        "Téléchargement du registre officiel de la Direction générale du Trésor (API publique gels-avoirs.dgtresor.gouv.fr), ingestion, delta et remplacement de la liste DGT active. Obligation autonome pour les établissements assujettis français (lignes directrices ACPR/DGT).": {"en": "Download of the official registry of the French Treasury (public API gels-avoirs.dgtresor.gouv.fr), ingestion, delta and replacement of the active DGT list. A standalone obligation for French regulated institutions (ACPR/DGT guidelines).", "de": "Download des offiziellen Registers des französischen Schatzamts (öffentliche API gels-avoirs.dgtresor.gouv.fr), Ingestion, Delta und Ersetzung der aktiven DGT-Liste. Eigenständige Pflicht für beaufsichtigte französische Institute (ACPR/DGT-Leitlinien).", "es": "Descarga del registro oficial del Tesoro francés (API pública gels-avoirs.dgtresor.gouv.fr), ingestión, delta y sustitución de la lista DGT activa. Obligación autónoma para las entidades francesas supervisadas (directrices ACPR/DGT).", "zh": "下载法国财政部官方登记册（公开 API gels-avoirs.dgtresor.gouv.fr），入库、计算增量并替换当前 DGT 名单。对受监管的法国机构而言是一项独立义务（ACPR/DGT 指引）。", "ar": "تنزيل السجل الرسمي للخزانة الفرنسية (API عامة gels-avoirs.dgtresor.gouv.fr) وإدخاله وحساب الفرق واستبدال قائمة DGT النشطة. التزام مستقل للمؤسسات الفرنسية الخاضعة للرقابة (إرشادات ACPR/DGT)."},
        "Téléchargement de la liste consolidée officielle (scsanctions.un.org, publique), ingestion, delta et remplacement de la liste ONU active.": {"en": "Download of the official consolidated list (scsanctions.un.org, public), ingestion, delta and replacement of the active UN list.", "de": "Download der offiziellen konsolidierten Liste (scsanctions.un.org, öffentlich), Ingestion, Delta und Ersetzung der aktiven UN-Liste.", "es": "Descarga de la lista consolidada oficial (scsanctions.un.org, pública), ingestión, delta y sustitución de la lista ONU activa.", "zh": "下载官方综合名单（scsanctions.un.org，公开），入库、计算增量并替换当前联合国名单。", "ar": "تنزيل القائمة الموحدة الرسمية (scsanctions.un.org، عامة) وإدخالها وحساب الفرق واستبدال قائمة الأمم المتحدة النشطة."},
        "Liste consolidée du HM Treasury (format 2022). Opt-in selon l'exposition UK (sync.ofsi.enabled).": {"en": "HM Treasury consolidated list (2022 format). Opt-in depending on UK exposure (sync.ofsi.enabled).", "de": "Konsolidierte Liste des HM Treasury (Format 2022). Opt-in je nach UK-Exposure (sync.ofsi.enabled).", "es": "Lista consolidada del HM Treasury (formato 2022). Opt-in según la exposición al Reino Unido (sync.ofsi.enabled).", "zh": "英国财政部综合名单（2022 格式）。根据英国风险敞口自行选择启用（sync.ofsi.enabled）。", "ar": "القائمة الموحدة لخزانة صاحبة الجلالة (صيغة 2022). تفعيل اختياري حسب التعرض للمملكة المتحدة (sync.ofsi.enabled)."},
        "Personnes Politiquement Exposées agrégées par OpenSanctions. Licence : usage non commercial libre, licence requise pour un usage commercial (sync.pep.enabled, désactivé par défaut).": {"en": "Politically Exposed Persons aggregated by OpenSanctions. License: free for non-commercial use, license required for commercial use (sync.pep.enabled, disabled by default).", "de": "Von OpenSanctions aggregierte politisch exponierte Personen. Lizenz: nicht-kommerzielle Nutzung frei, Lizenz für kommerzielle Nutzung erforderlich (sync.pep.enabled, standardmäßig deaktiviert).", "es": "Personas Políticamente Expuestas agregadas por OpenSanctions. Licencia: uso no comercial libre, licencia requerida para uso comercial (sync.pep.enabled, desactivado por defecto).", "zh": "由 OpenSanctions 汇总的政治公众人物。许可：非商业使用免费，商业使用需许可（sync.pep.enabled，默认禁用）。", "ar": "الأشخاص السياسيون المعرضون المجمعون من OpenSanctions. الترخيص: الاستخدام غير التجاري حر، ويلزم ترخيص للاستخدام التجاري (sync.pep.enabled، معطل افتراضياً)."},
        "Fichier consolidé des sanctions financières de l'UE (webgate FSD) : fait autorité sur le scraping du JO, les radiations y sont fiables. Nécessite un token gratuit (inscription au webgate, puis sync.eu_fsf.token dans config.yaml).": {"en": "Consolidated EU financial sanctions file (FSD webgate): authoritative over OJ scraping, delistings are reliable there. Requires a free token (webgate registration, then sync.eu_fsf.token in config.yaml).", "de": "Konsolidierte Datei der EU-Finanzsanktionen (FSD-Webgate): maßgeblich gegenüber dem OJ-Scraping, Streichungen sind dort verlässlich. Erfordert ein kostenloses Token (Webgate-Registrierung, dann sync.eu_fsf.token in config.yaml).", "es": "Archivo consolidado de sanciones financieras de la UE (webgate FSD): prevalece sobre el scraping del DO, las bajas son fiables. Requiere un token gratuito (registro en el webgate, luego sync.eu_fsf.token en config.yaml).", "zh": "欧盟金融制裁综合文件（FSD webgate）：比公报抓取更权威，除名信息可靠。需免费令牌（在 webgate 注册后将 sync.eu_fsf.token 写入 config.yaml）。", "ar": "الملف الموحد للعقوبات المالية للاتحاد الأوروبي (بوابة FSD): مرجعي مقارنة بكشط الجريدة الرسمية، والشطب فيه موثوق. يتطلب رمزاً مجانياً (التسجيل في البوابة ثم sync.eu_fsf.token في config.yaml)."},
        "Recherche des actes mentionnant « restrictive measures » au Journal Officiel (édition anglaise, qui fait référence), scraping des listés (Individus, Entités, Navires, Aéronefs), fusion incrémentale avec la liste EU active et archivage du PDF officiel de chaque acte (valeur probante en audit).": {"en": "Search for acts mentioning “restrictive measures” in the Official Journal (English edition, which is authoritative), scraping of listed parties (Individuals, Entities, Vessels, Aircraft), incremental merge with the active EU list and archiving of each act's official PDF (evidential value in audits).", "de": "Suche nach Rechtsakten mit „restrictive measures“ im Amtsblatt (englische, maßgebliche Ausgabe), Scraping der Gelisteten (Personen, Entitäten, Schiffe, Luftfahrzeuge), inkrementelle Zusammenführung mit der aktiven EU-Liste und Archivierung des offiziellen PDF jedes Rechtsakts (Beweiswert im Audit).", "es": "Búsqueda de actos que mencionan «restrictive measures» en el Diario Oficial (edición inglesa, que es la de referencia), scraping de los listados (personas, entidades, buques, aeronaves), fusión incremental con la lista UE activa y archivo del PDF oficial de cada acto (valor probatorio en auditoría).", "zh": "在官方公报（以英文版为准）中检索提及“restrictive measures”的法令，抓取列名对象（个人、实体、船舶、航空器），与当前欧盟名单增量合并，并归档每项法令的官方 PDF（审计证据价值）。", "ar": "البحث عن النصوص التي تذكر «restrictive measures» في الجريدة الرسمية (الطبعة الإنجليزية المرجعية)، وكشط المدرجين (أفراد، كيانات، سفن، طائرات)، والدمج التدريجي مع قائمة الاتحاد الأوروبي النشطة وأرشفة PDF الرسمي لكل نص (قيمة إثباتية في التدقيق)."},
        "Chaque source suit sa propre expression cron 5 champs (minute heure jour mois jour-de-semaine), modifiable à chaud (admin). Vide = retour au défaut (config.yaml ou horaire quotidien global). Exemples : 0 6 * * * (tous les jours à 6h), 0 */4 * * * (toutes les 4 h), 30 7 * * 1-5 (7h30 en semaine). Une même source ne se chevauche jamais.": {"en": "Each source follows its own 5-field cron expression (minute hour day month weekday), hot-editable (admin). Empty = back to default (config.yaml or the global daily time). Examples: 0 6 * * * (every day at 6am), 0 */4 * * * (every 4 hours), 30 7 * * 1-5 (7:30am on weekdays). A given source never overlaps itself.", "de": "Jede Quelle folgt ihrem eigenen 5-Feld-Cron-Ausdruck (Minute Stunde Tag Monat Wochentag), hot-editierbar (Admin). Leer = zurück zum Standard (config.yaml oder globale Tageszeit). Beispiele: 0 6 * * * (täglich 6 Uhr), 0 */4 * * * (alle 4 Stunden), 30 7 * * 1-5 (7:30 an Wochentagen). Eine Quelle überlappt sich nie selbst.", "es": "Cada fuente sigue su propia expresión cron de 5 campos (minuto hora día mes día-semana), editable en caliente (admin). Vacío = vuelta al valor por defecto (config.yaml u horario diario global). Ejemplos: 0 6 * * * (cada día a las 6h), 0 */4 * * * (cada 4 horas), 30 7 * * 1-5 (7:30 entre semana). Una misma fuente nunca se solapa.", "zh": "每个数据源遵循自己的 5 段 cron 表达式（分 时 日 月 周），管理员可热修改。留空 = 回退默认（config.yaml 或全局每日时间）。示例：0 6 * * *（每天 6 点），0 */4 * * *（每 4 小时），30 7 * * 1-5（工作日 7:30）。同一数据源永不自我重叠。", "ar": "كل مصدر يتبع تعبير cron الخاص به من 5 حقول (دقيقة ساعة يوم شهر يوم-أسبوع)، قابل للتعديل الفوري (مدير). فارغ = العودة للافتراضي. أمثلة: 0 6 * * * (يومياً الساعة 6)، 0 */4 * * * (كل 4 ساعات)، 30 7 * * 1-5 (7:30 أيام العمل). المصدر الواحد لا يتداخل مع نفسه أبداً."},
        "Suivi des exécutions manuelles et planifiées, avec le résumé du delta appliqué.": {"en": "Tracking of manual and scheduled runs, with the summary of the applied delta.", "de": "Verfolgung manueller und geplanter Läufe mit Zusammenfassung des angewendeten Deltas.", "es": "Seguimiento de las ejecuciones manuales y programadas, con el resumen del delta aplicado.", "zh": "跟踪手动与计划执行，并汇总已应用的增量。", "ar": "متابعة التنفيذات اليدوية والمجدولة، مع ملخص الفرق المطبق."},
        "Listes ingérées mais non encore mises en production. Traitez le snapshot le plus récent en premier : l'approbation remplace les snapshots antérieurs du même type.": {"en": "Lists ingested but not yet promoted to production. Handle the most recent snapshot first: approval supersedes earlier snapshots of the same type.", "de": "Ingestierte, aber noch nicht produktive Listen. Bearbeiten Sie den neuesten Snapshot zuerst: Die Freigabe ersetzt frühere Snapshots desselben Typs.", "es": "Listas ingeridas pero aún no puestas en producción. Trate primero la instantánea más reciente: la aprobación sustituye las instantáneas anteriores del mismo tipo.", "zh": "已入库但尚未投产的名单。请先处理最新快照：批准会替换同类型的早期快照。", "ar": "قوائم مُدخلة لكنها لم تُنشر بعد. عالج أحدث لقطة أولاً: الاعتماد يستبدل اللقطات السابقة من النوع نفسه."},
        "Vérifiez ce qui change par rapport à la liste actuellement en production : ajouts, modifications (avant → après) et suppressions de listés.": {"en": "Check what changes compared to the list currently in production: additions, modifications (before → after) and removals of listed parties.", "de": "Prüfen Sie, was sich gegenüber der aktuell produktiven Liste ändert: Zugänge, Änderungen (vorher → nachher) und Streichungen von Gelisteten.", "es": "Compruebe qué cambia respecto a la lista actualmente en producción: altas, modificaciones (antes → después) y bajas de listados.", "zh": "检查相对于当前在产名单的变化：新增、修改（前 → 后）与删除。", "ar": "تحقق مما يتغير مقارنة بالقائمة قيد الإنتاج: إضافات، تعديلات (قبل ← بعد) وحذف مدرجين."},
        "Excluez de la production les fiches non pertinentes pour votre établissement (faux positifs connus, périmètre hors activité). Chaque exclusion est justifiée selon les réglages.": {"en": "Exclude from production the records irrelevant to your institution (known false positives, out-of-scope perimeter). Every exclusion is justified according to the settings.", "de": "Schließen Sie für Ihr Institut irrelevante Einträge von der Produktion aus (bekannte Fehlalarme, Perimeter außerhalb der Geschäftstätigkeit). Jeder Ausschluss wird gemäß den Einstellungen begründet.", "es": "Excluya de la producción las fichas no pertinentes para su entidad (falsos positivos conocidos, perímetro fuera de actividad). Cada exclusión se justifica según los ajustes.", "zh": "将与本机构无关的记录排除出投产（已知误报、业务范围外）。每项排除均按设置要求说明理由。", "ar": "استبعد من الإنتاج السجلات غير الملائمة لمؤسستك (إنذارات كاذبة معروفة، نطاق خارج النشاط). كل استبعاد يُبرر وفق الإعدادات."},
        "Criblage à blanc d'un panel de pseudo-clients contre la liste actuelle ET la liste candidate — aucune alerte réelle n'est créée. L'écart de taux d'interception mesure l'impact de la nouvelle liste avant sa mise en production.": {"en": "Dry-run screening of a pseudo-client panel against the current list AND the candidate list — no real alert is created. The interception-rate gap measures the impact of the new list before it goes to production.", "de": "Trockenlauf-Screening eines Pseudo-Kunden-Panels gegen die aktuelle UND die Kandidatenliste — es entsteht kein echter Alarm. Die Abweichung der Trefferquote misst die Auswirkung der neuen Liste vor der Produktivsetzung.", "es": "Cribado en seco de un panel de pseudoclientes contra la lista actual Y la candidata — no se crea ninguna alerta real. La diferencia de tasa de intercepción mide el impacto de la nueva lista antes de su puesta en producción.", "zh": "用模拟客户面板对当前名单和候选名单进行空跑筛查——不产生任何真实警报。拦截率差异衡量新名单投产前的影响。", "ar": "فحص تجريبي للوحة عملاء وهميين ضد القائمة الحالية والقائمة المرشحة — لا يُنشأ أي تنبيه حقيقي. فرق معدل الاعتراض يقيس أثر القائمة الجديدة قبل نشرها."},
        "Renseignez les données du tiers pour évaluer son niveau de risque et de correspondance.": {"en": "Enter the third party's data to assess its risk and match level.", "de": "Erfassen Sie die Daten des Dritten, um Risiko- und Trefferniveau zu bewerten.", "es": "Introduzca los datos del tercero para evaluar su nivel de riesgo y de coincidencia.", "zh": "录入第三方数据以评估其风险与匹配程度。", "ar": "أدخل بيانات الطرف لتقييم مستوى المخاطر والتطابق."},
        "Par défaut, le criblage porte sur toutes les listes en production. Décochez pour restreindre le périmètre (ex. établissement non exposé au UK) — toute restriction est tracée dans le journal d'audit.": {"en": "By default, screening covers all lists in production. Untick to narrow the scope (e.g. an institution with no UK exposure) — any restriction is recorded in the audit trail.", "de": "Standardmäßig deckt das Screening alle produktiven Listen ab. Abwählen, um den Umfang einzuschränken (z. B. Institut ohne UK-Exposure) — jede Einschränkung wird im Audit-Trail vermerkt.", "es": "Por defecto, el cribado cubre todas las listas en producción. Desmarque para restringir el perímetro (p. ej. entidad sin exposición al Reino Unido) — toda restricción queda registrada en la pista de auditoría.", "zh": "默认对所有在产名单进行筛查。取消勾选可缩小范围（如无英国敞口的机构）——任何限制均记入审计轨迹。", "ar": "افتراضياً، يشمل الفحص كل القوائم قيد الإنتاج. ألغ التحديد لتضييق النطاق — أي تقييد يُسجل في سجل التدقيق."},
        "Un fichier CSV de clients (colonnes CLIENT_BASE) est criblé côté serveur en tâche de fond, avec les mêmes garanties que le temps réel : quality gate, liste blanche, règles anti-faux positifs, journal d'audit immuable et alertes. Les fichiers déposés par un moniteur de transfert (CFT/SFTP) dans l'inbox surveillée (batch.inbox_dir) apparaissent ici automatiquement.": {"en": "A client CSV file (CLIENT_BASE columns) is screened server-side in the background, with the same guarantees as real time: quality gate, whitelist, false-positive rules, immutable audit trail and alerts. Files dropped by a transfer monitor (CFT/SFTP) into the watched inbox (batch.inbox_dir) appear here automatically.", "de": "Eine Kunden-CSV-Datei (CLIENT_BASE-Spalten) wird serverseitig im Hintergrund gescreent, mit denselben Garantien wie in Echtzeit: Quality Gate, Whitelist, Fehlalarm-Regeln, unveränderlicher Audit-Trail und Alarme. Von einem Transfermonitor (CFT/SFTP) in die überwachte Inbox (batch.inbox_dir) abgelegte Dateien erscheinen hier automatisch.", "es": "Un archivo CSV de clientes (columnas CLIENT_BASE) se criba en el servidor en segundo plano, con las mismas garantías que el tiempo real: quality gate, lista blanca, reglas antifalsos positivos, pista de auditoría inmutable y alertas. Los archivos depositados por un monitor de transferencia (CFT/SFTP) en la bandeja vigilada (batch.inbox_dir) aparecen aquí automáticamente.", "zh": "客户 CSV 文件（CLIENT_BASE 列）在服务器后台筛查，与实时筛查同等保障：质量门控、白名单、误报规则、不可变审计轨迹与警报。由传输监控（CFT/SFTP）投入监控收件箱（batch.inbox_dir）的文件会自动出现在这里。", "ar": "ملف CSV للعملاء (أعمدة CLIENT_BASE) يُفحص على الخادم في الخلفية بنفس ضمانات الوقت الفعلي: بوابة الجودة، القائمة البيضاء، قواعد الإنذارات الكاذبة، سجل تدقيق ثابت وتنبيهات. الملفات المودعة عبر مراقب نقل (CFT/SFTP) في صندوق الوارد المراقب تظهر هنا تلقائياً."},
        "Soumettez une liste de clients pour exécuter le criblage par blocage et extraire les alertes (simule le traitement distribué Spark).": {"en": "Submit a list of clients to run blocking-based screening and extract the alerts (simulates distributed Spark processing).", "de": "Reichen Sie eine Kundenliste ein, um das Blocking-Screening auszuführen und die Alarme zu extrahieren (simuliert verteilte Spark-Verarbeitung).", "es": "Envíe una lista de clientes para ejecutar el cribado por bloqueo y extraer las alertas (simula el procesamiento distribuido Spark).", "zh": "提交客户列表以执行阻断筛查并提取警报（模拟 Spark 分布式处理）。", "ar": "أرسل قائمة عملاء لتنفيذ الفحص بالحجب واستخراج التنبيهات (يحاكي معالجة Spark الموزعة)."},
        "Périmètre des listes criblées (défaut : toutes — toute restriction est tracée dans l'audit) :": {"en": "Scope of the screened lists (default: all — any restriction is recorded in the audit trail):", "de": "Umfang der gescreenten Listen (Standard: alle — jede Einschränkung wird im Audit vermerkt):", "es": "Perímetro de las listas cribadas (por defecto: todas — toda restricción queda registrada en la auditoría):", "zh": "筛查名单范围（默认：全部 — 任何限制均记入审计）：", "ar": "نطاق القوائم المفحوصة (افتراضياً: الكل — أي تقييد يُسجل في التدقيق):"},
        "Soumettez un message de paiement pain.001 (ordre de virement client) ou pacs.008 (virement interbancaire) : toutes les parties du message — donneur d'ordre, bénéficiaire, ultimes, banques — sont criblées contre les listes en production. Verdict PASS ou HIT ; chaque partie criblée est tracée dans le journal d'audit et chaque hit ouvre une alerte de travail.": {"en": "Submit a pain.001 payment message (customer credit transfer) or pacs.008 (interbank transfer): every party in the message — originator, beneficiary, ultimates, banks — is screened against the lists in production. Verdict PASS or HIT; every screened party is recorded in the audit trail and every hit opens a work alert.", "de": "Reichen Sie eine Zahlungsnachricht pain.001 (Kundenüberweisung) oder pacs.008 (Interbankenüberweisung) ein: Alle Parteien der Nachricht — Auftraggeber, Begünstigter, Ultimates, Banken — werden gegen die produktiven Listen gescreent. Verdikt PASS oder HIT; jede gescreente Partei wird im Audit-Trail vermerkt und jeder Hit öffnet einen Arbeitsalarm.", "es": "Envíe un mensaje de pago pain.001 (transferencia de cliente) o pacs.008 (transferencia interbancaria): todas las partes del mensaje — ordenante, beneficiario, últimos, bancos — se criban contra las listas en producción. Veredicto PASS o HIT; cada parte cribada queda en la pista de auditoría y cada hit abre una alerta de trabajo.", "zh": "提交 pain.001（客户转账指令）或 pacs.008（银行间转账）支付报文：报文中的所有当事方——付款人、收款人、最终方、银行——均与在产名单比对。结论为 PASS 或 HIT；每个被筛查的当事方均记入审计轨迹，每次命中均开启工作警报。", "ar": "أرسل رسالة دفع pain.001 (تحويل عميل) أو pacs.008 (تحويل بين البنوك): جميع أطراف الرسالة — الآمر، المستفيد، النهائيون، البنوك — تُفحص ضد القوائم قيد الإنتاج. الحكم PASS أو HIT؛ كل طرف مفحوص يُسجل في سجل التدقيق وكل إصابة تفتح تنبيه عمل."},
        "Alertes issues du criblage du référentiel clients contre les listes. Chaque alerte s'instruit : assignation, analyse, proposition de décision (vrai/faux positif) puis validation 4-yeux par un réviseur différent du proposeur. Les alertes clôturées automatiquement par une règle anti-faux positifs (CLOSED_BY_RULE) restent visibles et auditables.": {"en": "Alerts from screening the client referential against the lists. Each alert is investigated: assignment, analysis, decision proposal (true/false positive) then 4-eyes validation by a reviewer different from the proposer. Alerts auto-closed by a false-positive rule (CLOSED_BY_RULE) remain visible and auditable.", "de": "Alarme aus dem Screening des Kundenbestands gegen die Listen. Jeder Alarm wird bearbeitet: Zuweisung, Analyse, Entscheidungsvorschlag (echter/falscher Treffer), dann 4-Augen-Freigabe durch einen vom Vorschlagenden verschiedenen Prüfer. Durch eine Fehlalarm-Regel automatisch geschlossene Alarme (CLOSED_BY_RULE) bleiben sichtbar und auditierbar.", "es": "Alertas del cribado del referencial de clientes contra las listas. Cada alerta se instruye: asignación, análisis, propuesta de decisión (verdadero/falso positivo) y validación a 4 ojos por un revisor distinto del proponente. Las alertas cerradas automáticamente por una regla antifalsos positivos (CLOSED_BY_RULE) siguen visibles y auditables.", "zh": "客户参照库与名单比对产生的警报。每条警报需调查：指派、分析、提出处置建议（真/假阳性），再由与提议人不同的复核员四眼确认。被误报规则自动关闭的警报（CLOSED_BY_RULE）仍可见且可审计。", "ar": "تنبيهات فحص مرجع العملاء ضد القوائم. كل تنبيه يُحقق: إسناد، تحليل، اقتراح قرار (إيجابي حقيقي/كاذب) ثم تحقق بأربع أعين من مراجع مختلف عن المقترح. التنبيهات المغلقة تلقائياً بقاعدة (CLOSED_BY_RULE) تبقى مرئية وقابلة للتدقيق."},
        "Alertes issues du filtrage des parties des messages de paiement (pain.001 / pacs.008). Même cycle de vie 4-yeux que le criblage, avec des règles anti-faux positifs et un blocking key propres à ce canal.": {"en": "Alerts from filtering the parties of payment messages (pain.001 / pacs.008). Same 4-eyes lifecycle as screening, with false-positive rules and a blocking key specific to this channel.", "de": "Alarme aus der Filterung der Parteien von Zahlungsnachrichten (pain.001 / pacs.008). Gleicher 4-Augen-Lebenszyklus wie das Screening, mit kanalspezifischen Fehlalarm-Regeln und Blocking-Schlüssel.", "es": "Alertas del filtrado de las partes de los mensajes de pago (pain.001 / pacs.008). Mismo ciclo de vida a 4 ojos que el cribado, con reglas antifalsos positivos y una clave de bloqueo propias de este canal.", "zh": "支付报文当事方过滤产生的警报（pain.001 / pacs.008）。与筛查相同的四眼生命周期，但拥有本通道专属的误报规则与阻断键。", "ar": "تنبيهات تصفية أطراف رسائل الدفع (pain.001 / pacs.008). نفس دورة حياة الأربع أعين، مع قواعد ومفتاح حجب خاصين بهذه القناة."},
        "Composantes ordonnées de la clé de blocking qui sélectionne les candidats du criblage. Toute modification recharge immédiatement le cache de production.": {"en": "Ordered components of the blocking key that selects screening candidates. Any change immediately reloads the production cache.", "de": "Geordnete Komponenten des Blocking-Schlüssels, der die Screening-Kandidaten auswählt. Jede Änderung lädt den Produktionscache sofort neu.", "es": "Componentes ordenados de la clave de bloqueo que selecciona los candidatos del cribado. Todo cambio recarga de inmediato la caché de producción.", "zh": "选择筛查候选的阻断键有序组件。任何修改都会立即重载生产缓存。", "ar": "مكونات مرتبة لمفتاح الحجب الذي يختار مرشحي الفحص. أي تعديل يعيد تحميل ذاكرة الإنتاج فوراً."},
        "Clé propre au filtrage des paiements. Par défaut réduite à la phonétique (les données de paiement sont pauvres) ; le type PP/PM est testé dans les deux variantes.": {"en": "Key specific to payment filtering. Reduced to phonetics by default (payment data is poor); the PP/PM type is tested in both variants.", "de": "Schlüssel speziell für die Zahlungsfilterung. Standardmäßig auf Phonetik reduziert (Zahlungsdaten sind arm); der PP/PM-Typ wird in beiden Varianten getestet.", "es": "Clave propia del filtrado de pagos. Por defecto reducida a la fonética (los datos de pago son pobres); el tipo PP/PM se prueba en ambas variantes.", "zh": "支付过滤专用键。默认仅保留语音组件（支付数据贫乏）；PP/PM 类型两种变体都会测试。", "ar": "مفتاح خاص بتصفية المدفوعات. مختزل افتراضياً إلى الصوتيات (بيانات الدفع فقيرة)؛ نوع PP/PM يُختبر بكلتا الصيغتين."},
        "Chaque règle est du code Python (def rule(ctx) -> bool) qui, en production, supprime les alertes correspondantes (auto-clôture CLOSED_BY_RULE, toujours tracée à l'audit). Cycle de vie façon branche : Brouillon → tests unitaires verts → Soumission → validation 4-yeux → Production. Une règle en production n'est jamais modifiée directement : « Modifier » crée une nouvelle version brouillon.": {"en": "Each rule is Python code (def rule(ctx) -> bool) which, in production, suppresses the matching alerts (auto-close CLOSED_BY_RULE, always recorded in the audit). Branch-style lifecycle: Draft → green unit tests → Submission → 4-eyes validation → Production. A production rule is never edited directly: “Edit” creates a new draft version.", "de": "Jede Regel ist Python-Code (def rule(ctx) -> bool), der in Produktion die passenden Alarme unterdrückt (Auto-Schließung CLOSED_BY_RULE, stets im Audit vermerkt). Branch-artiger Lebenszyklus: Entwurf → grüne Unit-Tests → Einreichung → 4-Augen-Freigabe → Produktion. Eine produktive Regel wird nie direkt geändert: „Bearbeiten“ erzeugt eine neue Entwurfsversion.", "es": "Cada regla es código Python (def rule(ctx) -> bool) que, en producción, suprime las alertas correspondientes (autocierre CLOSED_BY_RULE, siempre registrado en la auditoría). Ciclo de vida tipo rama: Borrador → tests unitarios verdes → Envío → validación a 4 ojos → Producción. Una regla en producción nunca se modifica directamente: «Editar» crea una nueva versión borrador.", "zh": "每条规则都是 Python 代码（def rule(ctx) -> bool），在生产中抑制匹配的警报（自动关闭 CLOSED_BY_RULE，始终记入审计）。分支式生命周期：草稿 → 单元测试全绿 → 提交 → 四眼确认 → 生产。生产规则从不直接修改：“编辑”会创建新草稿版本。", "ar": "كل قاعدة هي كود Python (def rule(ctx) -> bool) تحذف في الإنتاج التنبيهات المطابقة (إغلاق تلقائي CLOSED_BY_RULE، مسجل دائماً). دورة حياة على نمط الفروع: مسودة ← اختبارات خضراء ← تقديم ← تحقق بأربع أعين ← إنتاج. القاعدة المنشورة لا تعدل مباشرة: «تحرير» ينشئ نسخة مسودة جديدة."},
        "Suppression gouvernée des faux positifs récurrents : une paire active supprime les alertes futures de ce couple, mais chaque suppression reste tracée dans le journal d'audit (statut WHITELISTED). Révocation douce uniquement, avec motif.": {"en": "Governed suppression of recurring false positives: an active pair suppresses future alerts for that couple, but every suppression remains recorded in the audit trail (WHITELISTED status). Soft revocation only, with a reason.", "de": "Kontrollierte Unterdrückung wiederkehrender Fehlalarme: Ein aktives Paar unterdrückt künftige Alarme dieses Duos, jede Unterdrückung bleibt jedoch im Audit-Trail vermerkt (Status WHITELISTED). Nur weiche Widerrufung, mit Begründung.", "es": "Supresión gobernada de falsos positivos recurrentes: un par activo suprime las alertas futuras de esa pareja, pero cada supresión queda registrada en la pista de auditoría (estado WHITELISTED). Solo revocación suave, con motivo.", "zh": "对反复误报的受控抑制：活动配对会抑制该组合的未来警报，但每次抑制仍记入审计轨迹（状态 WHITELISTED）。仅限软撤销，须说明理由。", "ar": "حذف مُحوكم للإنذارات الكاذبة المتكررة: الزوج النشط يحذف التنبيهات المستقبلية لهذا الثنائي، لكن كل حذف يبقى مسجلاً في سجل التدقيق (حالة WHITELISTED). إلغاء مرن فقط مع بيان السبب."},
        "Volumes d'alertes, taux de faux positifs, délai moyen de décision, liste blanche active, listes en production et historique des synchronisations.": {"en": "Alert volumes, false-positive rate, average decision time, active whitelist, lists in production and synchronization history.", "de": "Alarmvolumen, Fehlalarmquote, durchschnittliche Entscheidungszeit, aktive Whitelist, produktive Listen und Synchronisationshistorie.", "es": "Volúmenes de alertas, tasa de falsos positivos, plazo medio de decisión, lista blanca activa, listas en producción e historial de sincronizaciones.", "zh": "警报量、误报率、平均决策时长、活动白名单、在产名单与同步历史。", "ar": "أحجام التنبيهات، معدل الإنذارات الكاذبة، متوسط زمن القرار، القائمة البيضاء النشطة، القوائم قيد الإنتاج وتاريخ المزامنات."},
        "Alertes ouvertes par analyste assigné, ventilées par priorité, avec les retards SLA et la prochaine échéance — pour répartir le travail. Les alertes non assignées apparaissent en tête.": {"en": "Open alerts per assigned analyst, broken down by priority, with SLA overdue counts and the next deadline — to balance the workload. Unassigned alerts appear first.", "de": "Offene Alarme je zugewiesenem Analysten, aufgeschlüsselt nach Priorität, mit SLA-Überfälligkeiten und nächster Frist — zur Arbeitsverteilung. Nicht zugewiesene Alarme stehen oben.", "es": "Alertas abiertas por analista asignado, desglosadas por prioridad, con los retrasos SLA y el próximo vencimiento — para repartir el trabajo. Las alertas sin asignar aparecen primero.", "zh": "按指派分析员的未结警报，按优先级细分，含 SLA 逾期与最近期限——用于分配工作。未指派警报排在最前。", "ar": "التنبيهات المفتوحة حسب المحلل المسند، مفصلة حسب الأولوية، مع تأخيرات SLA والأجل القادم — لتوزيع العمل. غير المسندة تظهر أولاً."},
        "Synthèse réglementaire prête pour un contrôle : volumétrie de criblage, alertes et décisions, délais, liste blanche, synchronisations et campagnes batch. Export CSV ou impression PDF.": {"en": "Regulator-ready period summary: screening volumes, alerts and decisions, delays, whitelist, synchronizations and batch campaigns. CSV export or PDF printing.", "de": "Prüfungsbereite Periodenzusammenfassung: Screening-Volumen, Alarme und Entscheidungen, Fristen, Whitelist, Synchronisationen und Batch-Kampagnen. CSV-Export oder PDF-Druck.", "es": "Síntesis reglamentaria lista para un control: volumetría de cribado, alertas y decisiones, plazos, lista blanca, sincronizaciones y campañas por lotes. Exportación CSV o impresión PDF.", "zh": "可直接用于监管检查的期间汇总：筛查量、警报与决策、时效、白名单、同步与批量任务。CSV 导出或 PDF 打印。", "ar": "ملخص تنظيمي جاهز للرقابة: أحجام الفحص، التنبيهات والقرارات، الآجال، القائمة البيضاء، المزامنات وحملات الدفعات. تصدير CSV أو طباعة PDF."},
        "Historique immuable de toutes les décisions de criblage émises par le moteur. Conforme aux standards ACPR/AMF.": {"en": "Immutable history of every screening decision issued by the engine. Compliant with ACPR/AMF standards.", "de": "Unveränderliche Historie aller vom Engine getroffenen Screening-Entscheidungen. Konform mit ACPR/AMF-Standards.", "es": "Historial inmutable de todas las decisiones de cribado emitidas por el motor. Conforme a los estándares ACPR/AMF.", "zh": "引擎所有筛查决策的不可变历史。符合 ACPR/AMF 标准。", "ar": "تاريخ ثابت لكل قرارات الفحص الصادرة عن المحرك. مطابق لمعايير ACPR/AMF."},
        "Trace immuable (append-only) des actions d'administration : création/modification/suppression de comptes, changements de réglages (avant → après), purges de snapshots et révocations de liste blanche. Attendu en contrôle ACPR/FED.": {"en": "Immutable (append-only) trace of administration actions: account creation/update/deletion, settings changes (before → after), snapshot purges and whitelist revocations. Expected in ACPR/FED examinations.", "de": "Unveränderliche (append-only) Spur der Admin-Aktionen: Konto-Anlage/-Änderung/-Löschung, Einstellungsänderungen (vorher → nachher), Snapshot-Löschungen und Whitelist-Widerrufe. In ACPR/FED-Prüfungen erwartet.", "es": "Traza inmutable (append-only) de las acciones de administración: creación/modificación/eliminación de cuentas, cambios de ajustes (antes → después), purgas de instantáneas y revocaciones de lista blanca. Esperada en un control ACPR/FED.", "zh": "管理操作的不可变（仅追加）记录：账户创建/修改/删除、设置变更（前 → 后）、快照清除与白名单撤销。ACPR/FED 检查的必查项。", "ar": "أثر ثابت (إضافة فقط) لإجراءات الإدارة: إنشاء/تعديل/حذف الحسابات، تغييرات الإعدادات (قبل ← بعد)، حذف اللقطات وإلغاء القائمة البيضاء. مطلوب في رقابة ACPR/FED."},
        "Réglages transverses du dispositif, modifiables à chaud sans redémarrage (stockés en base, config.yaml ne fournit que les défauts). Ils gouvernent l'homologation des listes, les exclusions, la validation 4-yeux des alertes, la liste blanche et le re-criblage automatique.": {"en": "Cross-cutting settings of the programme, hot-editable without restart (stored in the database, config.yaml only provides defaults). They govern list approval, exclusions, 4-eyes alert validation, the whitelist and automatic re-screening.", "de": "Übergreifende Einstellungen des Systems, hot-editierbar ohne Neustart (in der Datenbank gespeichert, config.yaml liefert nur die Standards). Sie steuern Listenfreigabe, Ausschlüsse, 4-Augen-Freigabe der Alarme, Whitelist und automatisches Re-Screening.", "es": "Ajustes transversales del dispositivo, editables en caliente sin reinicio (almacenados en base, config.yaml solo aporta los valores por defecto). Gobiernan la homologación de listas, las exclusiones, la validación a 4 ojos de las alertas, la lista blanca y el recribado automático.", "zh": "系统的横向设置，无需重启即可热修改（存于数据库，config.yaml 仅提供默认值）。它们管理名单审批、排除、警报四眼确认、白名单与自动重筛。", "ar": "إعدادات عرضية للمنظومة، قابلة للتعديل الفوري دون إعادة تشغيل (مخزنة في القاعدة، config.yaml يوفر الافتراضات فقط). تحكم اعتماد القوائم، الاستبعادات، تحقق الأربع أعين، القائمة البيضاء وإعادة الفحص التلقائي."},
        "Délai de traitement (heures) par priorité — l'échéance est calculée à la création de l'alerte, le retard est signalé dans la file. 0 = pas d'échéance.": {"en": "Handling time (hours) per priority — the deadline is computed when the alert is created, overdue alerts are flagged in the queue. 0 = no deadline.", "de": "Bearbeitungszeit (Stunden) je Priorität — die Frist wird bei Alarm-Erstellung berechnet, Überfälligkeit wird in der Liste markiert. 0 = keine Frist.", "es": "Plazo de tratamiento (horas) por prioridad — el vencimiento se calcula al crear la alerta, el retraso se señala en la cola. 0 = sin vencimiento.", "zh": "按优先级的处理时限（小时）——期限在警报创建时计算，逾期在队列中标记。0 = 无期限。", "ar": "مهلة المعالجة (ساعات) حسب الأولوية — يُحسب الأجل عند إنشاء التنبيه، ويُشار للتأخير في القائمة. 0 = بلا أجل."},
        "Envoi par email (variables SMTP) et vers les webhooks de config.yaml notifications.webhooks. Jamais bloquant pour le criblage.": {"en": "Sent by email (SMTP variables) and to the webhooks in config.yaml notifications.webhooks. Never blocking for screening.", "de": "Versand per E-Mail (SMTP-Variablen) und an die Webhooks aus config.yaml notifications.webhooks. Blockiert das Screening nie.", "es": "Envío por correo (variables SMTP) y a los webhooks de config.yaml notifications.webhooks. Nunca bloqueante para el cribado.", "zh": "通过邮件（SMTP 变量）与 config.yaml notifications.webhooks 的 webhook 发送。永不阻塞筛查。", "ar": "إرسال بالبريد (متغيرات SMTP) وإلى webhooks في config.yaml. لا يعرقل الفحص أبداً."},
        "Synthèse envoyée par email/webhooks à heure fixe (expression cron 5 champs) : files ouvertes par canal, retards SLA, 4-yeux en attente, homologations, volumétrie 24 h et santé des synchronisations.": {"en": "Summary sent by email/webhooks at a fixed time (5-field cron expression): open queues per channel, SLA overdue, pending 4-eyes, approvals, 24h volumes and synchronization health.", "de": "Zusammenfassung per E-Mail/Webhooks zu fester Zeit (5-Feld-Cron-Ausdruck): offene Listen je Kanal, SLA-Überfälligkeiten, ausstehende 4-Augen-Prüfungen, Freigaben, 24h-Volumen und Synchronisationsstatus.", "es": "Síntesis enviada por correo/webhooks a hora fija (expresión cron de 5 campos): colas abiertas por canal, retrasos SLA, 4 ojos pendientes, homologaciones, volumetría 24 h y salud de las sincronizaciones.", "zh": "按固定时间（5 段 cron 表达式）通过邮件/webhook 发送的汇总：各通道未结队列、SLA 逾期、待四眼确认、待审批、24 小时量与同步健康度。", "ar": "ملخص يُرسل بالبريد/webhooks في وقت ثابت (تعبير cron من 5 حقول): القوائم المفتوحة حسب القناة، تأخيرات SLA، الأربع أعين المعلقة، الاعتمادات، أحجام 24 ساعة وصحة المزامنات."},
        "Code à usage unique (TOTP, RFC 6238) demandé à chaque connexion, généré par une application d'authentification (Google Authenticator, Aegis, FreeOTP…). En cas de téléphone perdu, un administrateur peut réinitialiser la MFA du compte.": {"en": "One-time code (TOTP, RFC 6238) requested at every login, generated by an authenticator app (Google Authenticator, Aegis, FreeOTP…). If the phone is lost, an administrator can reset the account's MFA.", "de": "Einmalcode (TOTP, RFC 6238), bei jeder Anmeldung abgefragt, erzeugt von einer Authenticator-App (Google Authenticator, Aegis, FreeOTP…). Bei Telefonverlust kann ein Administrator die MFA des Kontos zurücksetzen.", "es": "Código de un solo uso (TOTP, RFC 6238) solicitado en cada inicio de sesión, generado por una aplicación de autenticación (Google Authenticator, Aegis, FreeOTP…). Si se pierde el teléfono, un administrador puede restablecer la MFA de la cuenta.", "zh": "每次登录需输入的一次性验证码（TOTP，RFC 6238），由身份验证器应用生成（Google Authenticator、Aegis、FreeOTP…）。手机丢失时，管理员可重置该账户的 MFA。", "ar": "رمز لمرة واحدة (TOTP، RFC 6238) يُطلب عند كل تسجيل دخول، يولده تطبيق مصادقة (Google Authenticator، Aegis، FreeOTP…). عند فقدان الهاتف، يمكن للمدير إعادة تعيين MFA للحساب."},
        "Durée de conservation (jours) par famille — 0 = conservation illimitée (défaut), minimum 30 jours quand une purge est activée. La purge tourne chaque jour à l'heure indiquée et chaque exécution est tracée RETENTION_PURGE. Le journal des actions d'administration n'est jamais purgé ; les décisions de criblage encore liées à une alerte conservée ne sont jamais supprimées.": {"en": "Retention (days) per family — 0 = keep forever (default), 30-day minimum when a purge is enabled. The purge runs daily at the indicated time and every run is recorded as RETENTION_PURGE. The administration action log is never purged; screening decisions still linked to a kept alert are never deleted.", "de": "Aufbewahrung (Tage) je Familie — 0 = unbegrenzt (Standard), mindestens 30 Tage bei aktivierter Löschung. Die Löschung läuft täglich zur angegebenen Zeit, jeder Lauf wird als RETENTION_PURGE vermerkt. Das Admin-Protokoll wird nie gelöscht; Screening-Entscheidungen, die noch mit einem aufbewahrten Alarm verknüpft sind, werden nie entfernt.", "es": "Duración de conservación (días) por familia — 0 = conservación ilimitada (por defecto), mínimo 30 días cuando una purga está activada. La purga se ejecuta cada día a la hora indicada y cada ejecución queda registrada como RETENTION_PURGE. El registro de acciones de administración nunca se purga; las decisiones de cribado aún vinculadas a una alerta conservada nunca se eliminan.", "zh": "按数据族的保留天数 — 0 = 永久保留（默认），启用清除时最少 30 天。清除每天按指定时间运行，每次执行记录为 RETENTION_PURGE。管理日志永不清除；仍与保留警报关联的筛查决策永不删除。", "ar": "مدة الاحتفاظ (أيام) لكل فئة — 0 = احتفاظ غير محدود (افتراضي)، وحد أدنى 30 يوماً عند تفعيل الحذف. يعمل الحذف يومياً في الوقت المحدد وكل تنفيذ يُسجل RETENTION_PURGE. سجل الإدارة لا يُحذف أبداً؛ وقرارات الفحص المرتبطة بتنبيه محفوظ لا تُحذف أبداً."},
        "Pendant votre absence, toute alerte qui vous serait assignée ira à votre délégué, et vos alertes ouvertes peuvent lui être réassignées immédiatement (chaque réassignation est tracée dans l'historique de l'alerte).": {"en": "While you are away, any alert that would be assigned to you goes to your delegate, and your open alerts can be reassigned to them immediately (each reassignment is recorded in the alert's history).", "de": "Während Ihrer Abwesenheit geht jeder Ihnen zugewiesene Alarm an Ihre Vertretung, und Ihre offenen Alarme können ihr sofort zugewiesen werden (jede Neuzuweisung wird in der Alarmhistorie vermerkt).", "es": "Durante su ausencia, toda alerta que se le asignaría irá a su delegado, y sus alertas abiertas pueden reasignársele de inmediato (cada reasignación queda registrada en el historial de la alerta).", "zh": "您缺勤期间，指派给您的警报将转给代理人，您的未结警报也可立即转指派给他（每次转指派均记入警报历史）。", "ar": "أثناء غيابك، كل تنبيه كان سيُسند إليك سيذهب إلى مفوضك، ويمكن إعادة إسناد تنبيهاتك المفتوحة إليه فوراً (كل إعادة إسناد تُسجل في تاريخ التنبيه)."},
        "Seuil de cut-off global et surcharges par type de liste, appliqués à chaud au criblage clients et au filtrage transactionnel (prioritaires sur config.yaml). Surcharge vide = seuil global. Toute modification est journalisée.": {"en": "Global cut-off threshold and per-list overrides, applied hot to client screening and transaction filtering (they override config.yaml). Empty override = global threshold. Every change is logged.", "de": "Globaler Cut-off-Schwellenwert und listenspezifische Übersteuerungen, hot angewendet auf Kunden-Screening und Transaktionsfilterung (haben Vorrang vor config.yaml). Leere Übersteuerung = globaler Schwellenwert. Jede Änderung wird protokolliert.", "es": "Umbral de corte global y sobrescrituras por tipo de lista, aplicados en caliente al cribado de clientes y al filtrado transaccional (prevalecen sobre config.yaml). Sobrescritura vacía = umbral global. Todo cambio se registra.", "zh": "全局截断阈值与按名单类型的覆盖值，热应用于客户筛查与交易过滤（优先于 config.yaml）。覆盖值留空 = 使用全局阈值。每次修改均记入日志。", "ar": "عتبة القطع العامة وتجاوزات حسب نوع القائمة، تطبق فورياً على فحص العملاء وتصفية المعاملات (لها الأولوية على config.yaml). تجاوز فارغ = العتبة العامة. كل تعديل يُسجل."},
        "Export/import JSON des réglages à chaud (homologation, 4-yeux, blocking keys, planification cron, SLA, notifications, rétention…) pour aligner recette et production. Aucun secret ne transite : ni comptes, ni clés d'API. Chaque import est journalisé SETTINGS_IMPORTED avec le delta.": {"en": "JSON export/import of the hot settings (approval, 4-eyes, blocking keys, cron schedules, SLA, notifications, retention…) to align staging and production. No secret ever transits: no accounts, no API keys. Every import is logged as SETTINGS_IMPORTED with the delta.", "de": "JSON-Export/-Import der Hot-Einstellungen (Freigabe, 4-Augen, Blocking-Schlüssel, Cron-Pläne, SLA, Benachrichtigungen, Aufbewahrung…) zum Abgleich von Test und Produktion. Kein Geheimnis wird übertragen: keine Konten, keine API-Schlüssel. Jeder Import wird als SETTINGS_IMPORTED mit Delta protokolliert.", "es": "Exportación/importación JSON de los ajustes en caliente (homologación, 4 ojos, claves de bloqueo, planificación cron, SLA, notificaciones, retención…) para alinear preproducción y producción. Ningún secreto transita: ni cuentas ni claves de API. Cada importación se registra como SETTINGS_IMPORTED con el delta.", "zh": "热设置的 JSON 导出/导入（审批、四眼、阻断键、cron 计划、SLA、通知、保留…），用于对齐测试与生产环境。不传输任何机密：无账户、无 API 密钥。每次导入记录为 SETTINGS_IMPORTED 并含差异。", "ar": "تصدير/استيراد JSON للإعدادات الفورية (اعتماد، أربع أعين، مفاتيح الحجب، جداول cron، SLA، إشعارات، احتفاظ…) لمحاذاة بيئتي الاختبار والإنتاج. لا يمر أي سر: لا حسابات ولا مفاتيح API. كل استيراد يُسجل SETTINGS_IMPORTED مع الفرق."},
        "Comptes de service pour les intégrations systèmes (CFT, ordonnanceur, SI amont) : authentification par en-tête X-API-Key, rôles restreints (jamais admin), révocation immédiate, usage tracé. La clé complète n'est affichée qu'à la création.": {"en": "Service accounts for system integrations (CFT, scheduler, upstream IS): X-API-Key header authentication, restricted roles (never admin), immediate revocation, usage tracked. The full key is only shown at creation.", "de": "Servicekonten für Systemintegrationen (CFT, Scheduler, vorgelagerte IT): Authentifizierung per X-API-Key-Header, eingeschränkte Rollen (nie Admin), sofortige Widerrufung, Nutzung protokolliert. Der vollständige Schlüssel wird nur bei der Erstellung angezeigt.", "es": "Cuentas de servicio para las integraciones de sistemas (CFT, planificador, SI aguas arriba): autenticación por cabecera X-API-Key, roles restringidos (nunca admin), revocación inmediata, uso trazado. La clave completa solo se muestra al crearla.", "zh": "系统集成的服务账户（CFT、调度器、上游系统）：X-API-Key 头部认证，受限角色（永不为 admin），即时吊销，使用可追溯。完整密钥仅在创建时显示一次。", "ar": "حسابات خدمة لتكاملات الأنظمة (CFT، مجدول، أنظمة علوية): مصادقة برأس X-API-Key، أدوار مقيدة (ليست admin أبداً)، إلغاء فوري، واستخدام مُتتبع. المفتاح الكامل يُعرض مرة واحدة عند الإنشاء فقط."},
        "Administration des utilisateurs du système, création de comptes et gestion des rôles.": {"en": "Administration of system users, account creation and role management.", "de": "Verwaltung der Systembenutzer, Kontoanlage und Rollenverwaltung.", "es": "Administración de los usuarios del sistema, creación de cuentas y gestión de roles.", "zh": "系统用户管理、账户创建与角色管理。", "ar": "إدارة مستخدمي النظام وإنشاء الحسابات وإدارة الأدوار."},
        "Points de contrôle affichés dans le dossier d'investigation de chaque alerte (un par ligne, 20 maximum). Chaque coche est tracée dans l'historique append-only de l'alerte. Vider le champ = retour à la checklist par défaut.": {"en": "Control points shown in each alert's investigation case file (one per line, 20 max). Every tick is recorded in the alert's append-only history. Empty the field = back to the default checklist.", "de": "Kontrollpunkte im Ermittlungsdossier jedes Alarms (einer pro Zeile, max. 20). Jedes Häkchen wird in der Append-only-Historie des Alarms vermerkt. Feld leeren = zurück zur Standard-Checkliste.", "es": "Puntos de control mostrados en el expediente de investigación de cada alerta (uno por línea, 20 máx.). Cada marca queda registrada en el historial append-only de la alerta. Vaciar el campo = volver a la lista por defecto.", "zh": "显示在每条警报调查卷宗中的检查点（每行一项，最多 20 项）。每次勾选都记入警报的仅追加历史。清空字段 = 恢复默认清单。", "ar": "نقاط المراقبة المعروضة في ملف التحقيق لكل تنبيه (واحدة في كل سطر، بحد أقصى 20). كل علامة تُسجل في تاريخ التنبيه. إفراغ الحقل = العودة للقائمة الافتراضية."},
        "Complétude des champs KYC du référentiel clients en production : un dossier incomplet (date de naissance ou pays manquants) dégrade la précision du criblage et augmente les faux positifs. Barres vertes ≥ 95 %, orange ≥ 80 %, rouges en dessous.": {"en": "Completeness of the KYC fields in the production client repository: an incomplete file (missing date of birth or country) degrades screening precision and increases false positives. Green bars ≥ 95%, orange ≥ 80%, red below.", "de": "Vollständigkeit der KYC-Felder im produktiven Kundenbestand: eine unvollständige Akte (fehlendes Geburtsdatum oder Land) verschlechtert die Screening-Präzision und erhöht die Fehlalarme. Grüne Balken ≥ 95 %, orange ≥ 80 %, rot darunter.", "es": "Completitud de los campos KYC del repositorio de clientes en producción: un expediente incompleto (fecha de nacimiento o país ausentes) degrada la precisión del cribado y aumenta los falsos positivos. Barras verdes ≥ 95 %, naranjas ≥ 80 %, rojas por debajo.", "zh": "在产客户库 KYC 字段的完整度：档案不完整（缺出生日期或国家）会降低筛查精度并增加误报。绿色条 ≥ 95%，橙色 ≥ 80%，其下为红色。", "ar": "اكتمال حقول اعرف عميلك في مستودع العملاء المنشور: الملف الناقص (تاريخ ميلاد أو بلد مفقود) يقلل دقة الفحص ويزيد الإنذارات الكاذبة. الأشرطة الخضراء ≥ 95%، البرتقالية ≥ 80%، والحمراء دون ذلك."},
        "Deux assistants génèrent un brouillon de code dans l'éditeur ci-dessous — le circuit de gouvernance (tests, soumission, validation 4-yeux) reste inchangé.": {"en": "Two assistants generate a draft of code in the editor below — the governance workflow (tests, submission, four-eyes validation) is unchanged.", "de": "Zwei Assistenten erzeugen einen Code-Entwurf im Editor unten — der Governance-Ablauf (Tests, Einreichung, Vier-Augen-Freigabe) bleibt unverändert.", "es": "Dos asistentes generan un borrador de código en el editor de abajo — el circuito de gobernanza (pruebas, envío, validación a cuatro ojos) no cambia.", "zh": "两个助手在下方编辑器中生成代码草稿——治理流程（测试、提交、四眼验证）保持不变。", "ar": "يولّد مساعدان مسودة كود في المحرر أدناه — تبقى دورة الحوكمة (اختبارات، تقديم، تحقق رباعي الأعين) دون تغيير."},
    };

    // ---- Chaines composees (nombres variables) : regles regex ----
    const R = [
        [/^(\d+) élément\(s\) — page (\d+) \/ (\d+)$/, {
            en: "$1 item(s) — page $2 / $3", de: "$1 Element(e) — Seite $2 / $3",
            es: "$1 elemento(s) — página $2 / $3", zh: "$1 项 — 第 $2 / $3 页",
            ar: "$1 عنصر — صفحة $2 / $3" }],
        [/^(\d+) sélectionnée\(s\)$/, {
            en: "$1 selected", de: "$1 ausgewählt", es: "$1 seleccionada(s)",
            zh: "已选 $1 项", ar: "المحدد: $1" }],
        [/^(\d+) alerte\(s\) détectée\(s\)$/, {
            en: "$1 alert(s) detected", de: "$1 Alarm(e) erkannt",
            es: "$1 alerta(s) detectada(s)", zh: "检测到 $1 条警报", ar: "تم رصد $1 تنبيه" }],
    ];

    // Locale d'affichage des dates/nombres par langue (chiffres latins en arabe)
    const LOCALES = { fr: "fr-FR", en: "en-GB", de: "de-DE", es: "es-ES",
                      zh: "zh-CN", ar: "ar-SA-u-nu-latn" };

    // ---- Moteur ----
    const ATTRS = ["placeholder", "title", "aria-label"];

    function currentLang() {
        try {
            const lang = localStorage.getItem("fiskr_lang");
            return LANGS[lang] ? lang : "fr";
        } catch (e) { return "fr"; }
    }

    function lookup(raw, lang) {
        if (!raw) return null;
        const key = raw.replace(/\s+/g, " ").trim();
        if (!key) return null;
        const entry = T[key];
        const lead = raw.match(/^\s*/)[0];
        const tail = raw.match(/\s*$/)[0];
        if (entry && entry[lang]) {
            return lead + entry[lang] + tail;
        }
        for (const [pattern, templates] of R) {
            const m = key.match(pattern);
            if (m && templates[lang]) {
                return lead + templates[lang].replace(/\$(\d)/g, (_, i) => m[Number(i)]) + tail;
            }
        }
        return null;
    }

    function translateTree(root, lang) {
        if (lang === "fr" || !root) return;
        if (root.nodeType === 3) {
            const out = lookup(root.nodeValue, lang);
            if (out !== null) root.nodeValue = out;
            return;
        }
        if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
        // Paragraphes descriptifs entiers d'abord (les fragments traduits ne
        // matcheraient plus la cle francaise complete)
        const paragraphs = root.querySelectorAll ? root.querySelectorAll("p.section-desc") : [];
        const selfP = (root.nodeType === 1 && root.matches && root.matches("p.section-desc")) ? [root] : [];
        for (const p of [...selfP, ...paragraphs]) {
            const key = p.textContent.replace(/\s+/g, " ").trim();
            const entry = P[key];
            if (entry && entry[lang]) p.textContent = entry[lang];
        }
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                const parent = node.parentElement;
                if (!parent || parent.tagName === "SCRIPT" || parent.tagName === "STYLE") {
                    return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
            }
        });
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        for (const node of nodes) {
            const out = lookup(node.nodeValue, lang);
            if (out !== null) node.nodeValue = out;
        }
        const elements = root.querySelectorAll ? root.querySelectorAll("*") : [];
        const self = root.nodeType === 1 ? [root] : [];
        for (const el of [...self, ...elements]) {
            for (const attr of ATTRS) {
                const value = el.getAttribute && el.getAttribute(attr);
                if (value) {
                    const out = lookup(value, lang);
                    if (out !== null) el.setAttribute(attr, out);
                }
            }
        }
    }

    let observer = null;
    function startObserver(lang) {
        if (lang === "fr" || observer) return;
        const handle = (mutations) => {
            observer.disconnect();
            for (const mutation of mutations) {
                if (mutation.type === "characterData") {
                    const out = lookup(mutation.target.nodeValue, lang);
                    if (out !== null) mutation.target.nodeValue = out;
                } else {
                    for (const node of mutation.addedNodes) translateTree(node, lang);
                }
            }
            observe();
        };
        observer = new MutationObserver(handle);
        const observe = () => observer.observe(document.body, {
            childList: true, subtree: true, characterData: true,
        });
        observe();
    }

    function applyDirection(lang) {
        document.documentElement.lang = lang;
        document.documentElement.dir = (lang === "ar") ? "rtl" : "ltr";
    }

    function setLang(lang) {
        if (!LANGS[lang]) lang = "fr";
        try { localStorage.setItem("fiskr_lang", lang); } catch (e) { /* stockage indisponible */ }
        // Rechargement : la page repart du HTML source français et se traduit
        // proprement dans la nouvelle langue (pas de traduction sur traduction)
        location.reload();
    }

    function init() {
        const lang = currentLang();
        applyDirection(lang);
        const select = document.getElementById("lang-select");
        if (select) select.value = lang;
        translateTree(document.body, lang);
        startObserver(lang);
    }

    // Langue + sens d'ecriture appliques des le parsing (avant le premier rendu)
    applyDirection(currentLang());

    window.fiskrI18n = {
        LANGS,
        currentLang,
        setLang,
        locale() { return LOCALES[currentLang()] || "fr-FR"; },
        t(s) { const out = lookup(s, currentLang()); return out === null ? s : out; },
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
