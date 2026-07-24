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
    };

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
        if (entry && entry[lang]) {
            const lead = raw.match(/^\s*/)[0];
            const tail = raw.match(/\s*$/)[0];
            return lead + entry[lang] + tail;
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
        t(s) { const out = lookup(s, currentLang()); return out === null ? s : out; },
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
