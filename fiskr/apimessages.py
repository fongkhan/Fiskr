"""
Messages d'API multilingues (negociation Accept-Language).

Le backend continue d'emettre ses messages en francais (langue source) ; un
middleware traduit les champs `detail` et `message` des reponses JSON quand
le client prefere une autre langue supportee (en, de, es, zh, ar). Meme
principe que l'i18n du dashboard : correspondance exacte apres normalisation,
plus des gabarits regex pour les messages a variables. Toute chaine absente
du catalogue reste en francais (jamais de trou).
"""
import re
from typing import Any, Dict, Optional

SUPPORTED_LANGS = ("fr", "en", "de", "es", "zh", "ar")

# ---- Catalogue : « message français » -> { en, de, es, zh, ar } ----
MESSAGES: Dict[str, Dict[str, str]] = {
    # Authentification & session
    "Identifiants incorrects. Veuillez réessayer.": {
        "en": "Invalid credentials. Please try again.",
        "de": "Ungültige Anmeldedaten. Bitte erneut versuchen.",
        "es": "Credenciales incorrectas. Inténtelo de nuevo.",
        "zh": "凭据错误，请重试。", "ar": "بيانات اعتماد غير صحيحة. حاول مجدداً."},
    "Nom d'utilisateur et mot de passe requis.": {
        "en": "Username and password are required.",
        "de": "Benutzername und Passwort erforderlich.",
        "es": "Se requieren usuario y contraseña.",
        "zh": "需要用户名和密码。", "ar": "اسم المستخدم وكلمة المرور مطلوبان."},
    "Code de vérification requis (MFA activée sur ce compte).": {
        "en": "Verification code required (MFA is enabled on this account).",
        "de": "Bestätigungscode erforderlich (MFA ist für dieses Konto aktiv).",
        "es": "Código de verificación requerido (MFA activada en esta cuenta).",
        "zh": "需要验证码（此账户已启用 MFA）。", "ar": "رمز التحقق مطلوب (MFA مفعلة على هذا الحساب)."},
    "Code de vérification incorrect. Veuillez réessayer.": {
        "en": "Incorrect verification code. Please try again.",
        "de": "Falscher Bestätigungscode. Bitte erneut versuchen.",
        "es": "Código de verificación incorrecto. Inténtelo de nuevo.",
        "zh": "验证码错误，请重试。", "ar": "رمز تحقق غير صحيح. حاول مجدداً."},
    "Déconnexion réussie.": {
        "en": "Logged out successfully.", "de": "Erfolgreich abgemeldet.",
        "es": "Sesión cerrada correctamente.", "zh": "已成功退出登录。", "ar": "تم تسجيل الخروج بنجاح."},
    "Non authentifié. Veuillez vous connecter.": {
        "en": "Not authenticated. Please log in.", "de": "Nicht angemeldet. Bitte anmelden.",
        "es": "No autenticado. Inicie sesión.", "zh": "未认证，请登录。", "ar": "غير مصادق. الرجاء تسجيل الدخول."},
    "Jeton d'authentification invalide ou expiré.": {
        "en": "Invalid or expired authentication token.",
        "de": "Ungültiges oder abgelaufenes Authentifizierungstoken.",
        "es": "Token de autenticación no válido o caducado.",
        "zh": "认证令牌无效或已过期。", "ar": "رمز مصادقة غير صالح أو منتهي."},
    "Utilisateur introuvable.": {
        "en": "User not found.", "de": "Benutzer nicht gefunden.",
        "es": "Usuario no encontrado.", "zh": "未找到用户。", "ar": "المستخدم غير موجود."},
    "Clé d'API invalide ou révoquée.": {
        "en": "Invalid or revoked API key.", "de": "Ungültiger oder widerrufener API-Schlüssel.",
        "es": "Clave de API no válida o revocada.", "zh": "API 密钥无效或已吊销。", "ar": "مفتاح API غير صالح أو ملغى."},
    "Compte auditeur : accès en lecture seule.": {
        "en": "Auditor account: read-only access.", "de": "Auditor-Konto: nur Lesezugriff.",
        "es": "Cuenta de auditor: acceso de solo lectura.", "zh": "审计账户：只读访问。", "ar": "حساب مدقق: وصول للقراءة فقط."},
    "Accès refusé. Privilèges d'administrateur requis.": {
        "en": "Access denied. Administrator privileges required.",
        "de": "Zugriff verweigert. Administratorrechte erforderlich.",
        "es": "Acceso denegado. Se requieren privilegios de administrador.",
        "zh": "拒绝访问。需要管理员权限。", "ar": "الوصول مرفوض. صلاحيات مدير مطلوبة."},
    "Mot de passe incorrect.": {
        "en": "Incorrect password.", "de": "Falsches Passwort.",
        "es": "Contraseña incorrecta.", "zh": "密码错误。", "ar": "كلمة مرور غير صحيحة."},
    # MFA
    "La MFA est déjà activée sur ce compte.": {
        "en": "MFA is already enabled on this account.", "de": "MFA ist für dieses Konto bereits aktiv.",
        "es": "La MFA ya está activada en esta cuenta.", "zh": "此账户已启用 MFA。", "ar": "MFA مفعلة مسبقاً على هذا الحساب."},
    "MFA activée : un code sera demandé à chaque connexion.": {
        "en": "MFA enabled: a code will be requested at every login.",
        "de": "MFA aktiviert: Bei jeder Anmeldung wird ein Code abgefragt.",
        "es": "MFA activada: se pedirá un código en cada inicio de sesión.",
        "zh": "MFA 已启用：每次登录都需要验证码。", "ar": "تم تفعيل MFA: سيُطلب رمز عند كل تسجيل دخول."},
    "MFA désactivée.": {
        "en": "MFA disabled.", "de": "MFA deaktiviert.",
        "es": "MFA desactivada.", "zh": "MFA 已停用。", "ar": "تم تعطيل MFA."},
    "Code incorrect : vérifiez l'application d'authentification.": {
        "en": "Incorrect code: check your authenticator app.",
        "de": "Falscher Code: Prüfen Sie Ihre Authenticator-App.",
        "es": "Código incorrecto: compruebe su aplicación de autenticación.",
        "zh": "验证码错误：请检查身份验证器应用。", "ar": "رمز غير صحيح: تحقق من تطبيق المصادقة."},
    # Alertes
    "Alerte introuvable.": {
        "en": "Alert not found.", "de": "Alarm nicht gefunden.",
        "es": "Alerta no encontrada.", "zh": "未找到警报。", "ar": "التنبيه غير موجود."},
    "Le commentaire ne peut pas être vide.": {
        "en": "The comment cannot be empty.", "de": "Der Kommentar darf nicht leer sein.",
        "es": "El comentario no puede estar vacío.", "zh": "评论不能为空。", "ar": "لا يمكن أن يكون التعليق فارغاً."},
    "Commentaire ajouté.": {
        "en": "Comment added.", "de": "Kommentar hinzugefügt.",
        "es": "Comentario añadido.", "zh": "已添加评论。", "ar": "أضيف التعليق."},
    "Seul un administrateur peut assigner une alerte à un autre analyste.": {
        "en": "Only an administrator can assign an alert to another analyst.",
        "de": "Nur ein Administrator kann einen Alarm einem anderen Analysten zuweisen.",
        "es": "Solo un administrador puede asignar una alerta a otro analista.",
        "zh": "只有管理员才能把警报指派给其他分析员。", "ar": "المدير فقط يمكنه إسناد تنبيه لمحلل آخر."},
    "Seul un administrateur peut assigner des alertes à un autre analyste.": {
        "en": "Only an administrator can assign alerts to another analyst.",
        "de": "Nur ein Administrator kann Alarme einem anderen Analysten zuweisen.",
        "es": "Solo un administrador puede asignar alertas a otro analista.",
        "zh": "只有管理员才能把警报指派给其他分析员。", "ar": "المدير فقط يمكنه إسناد تنبيهات لمحلل آخر."},
    "Aucune alerte sélectionnée.": {
        "en": "No alert selected.", "de": "Kein Alarm ausgewählt.",
        "es": "Ninguna alerta seleccionada.", "zh": "未选择警报。", "ar": "لم يُحدد أي تنبيه."},
    "Action en masse inconnue (assign, priority ou propose).": {
        "en": "Unknown bulk action (assign, priority or propose).",
        "de": "Unbekannte Massenaktion (assign, priority oder propose).",
        "es": "Acción masiva desconocida (assign, priority o propose).",
        "zh": "未知的批量操作（assign、priority 或 propose）。", "ar": "إجراء جماعي غير معروف (assign أو priority أو propose)."},
    "La validation 4-yeux reste unitaire : chaque décision proposée se valide une par une, par un réviseur différent du proposeur.": {
        "en": "Four-eyes validation stays one at a time: each proposed decision is validated individually, by a reviewer other than the proposer.",
        "de": "Die Vier-Augen-Freigabe bleibt einzeln: jede vorgeschlagene Entscheidung wird einzeln freigegeben, von einem anderen Prüfer als dem Vorschlagenden.",
        "es": "La validación a cuatro ojos sigue siendo unitaria: cada decisión propuesta se valida una a una, por un revisor distinto del proponente.",
        "zh": "四眼复核仍须逐条进行：每项提议的决定都要单独复核，且复核人不得是提议人。",
        "ar": "يبقى تحقق الأربع أعين فرديًا: يُعتمد كل قرار مقترح على حدة، من قِبل مراجع غير صاحب الاقتراح."},
    "Aucune alerte à prendre : chaque alerte à instruire a déjà quelqu'un.": {
        "en": "No alert to take: every alert to be investigated already has someone.",
        "de": "Kein Alarm zu übernehmen: jeder zu bearbeitende Alarm hat bereits jemanden.",
        "es": "Ninguna alerta que tomar: cada alerta por instruir ya tiene a alguien.",
        "zh": "没有可领取的警报：每条待处理的警报都已有人负责。",
        "ar": "لا يوجد تنبيه لأخذه: لكل تنبيه قيد التحقيق شخص مسؤول بالفعل."},
    "Canal inconnu (SCREENING ou FILTERING).": {
        "en": "Unknown channel (SCREENING or FILTERING).", "de": "Unbekannter Kanal (SCREENING oder FILTERING).",
        "es": "Canal desconocido (SCREENING o FILTERING).", "zh": "未知通道（SCREENING 或 FILTERING）。",
        "ar": "قناة غير معروفة (SCREENING أو FILTERING)."},
    # Réglages & gouvernance
    "Aucun réglage fourni.": {
        "en": "No setting provided.", "de": "Keine Einstellung übermittelt.",
        "es": "Ningún ajuste proporcionado.", "zh": "未提供任何设置。", "ar": "لم يقدم أي إعداد."},
    "Réglages d'homologation mis à jour.": {
        "en": "Approval settings updated.", "de": "Freigabe-Einstellungen aktualisiert.",
        "es": "Ajustes de homologación actualizados.", "zh": "审批设置已更新。", "ar": "تم تحديث إعدادات الاعتماد."},
    "Politique de rétention mise à jour.": {
        "en": "Retention policy updated.", "de": "Aufbewahrungsrichtlinie aktualisiert.",
        "es": "Política de retención actualizada.", "zh": "保留策略已更新。", "ar": "تم تحديث سياسة الاحتفاظ."},
    "Rien à purger avec la politique actuelle.": {
        "en": "Nothing to purge with the current policy.",
        "de": "Mit der aktuellen Richtlinie nichts zu löschen.",
        "es": "Nada que purgar con la política actual.",
        "zh": "按当前策略无可清除项。", "ar": "لا شيء للحذف وفق السياسة الحالية."},
    "Seuils de score mis à jour (effet immédiat).": {
        "en": "Score thresholds updated (immediate effect).",
        "de": "Schwellenwerte aktualisiert (sofort wirksam).",
        "es": "Umbrales de puntuación actualizados (efecto inmediato).",
        "zh": "得分阈值已更新（立即生效）。", "ar": "تم تحديث عتبات الدرجات (بأثر فوري)."},
    "Le seuil global doit être entre 0 et 100.": {
        "en": "The global threshold must be between 0 and 100.",
        "de": "Der globale Schwellenwert muss zwischen 0 und 100 liegen.",
        "es": "El umbral global debe estar entre 0 y 100.",
        "zh": "全局阈值必须在 0 到 100 之间。", "ar": "يجب أن تكون العتبة العامة بين 0 و100."},
    # Vues & absence
    "Vue introuvable.": {
        "en": "View not found.", "de": "Ansicht nicht gefunden.",
        "es": "Vista no encontrada.", "zh": "未找到视图。", "ar": "طريقة العرض غير موجودة."},
    "Cette vue appartient à un autre utilisateur.": {
        "en": "This view belongs to another user.", "de": "Diese Ansicht gehört einem anderen Benutzer.",
        "es": "Esta vista pertenece a otro usuario.", "zh": "此视图属于其他用户。", "ar": "طريقة العرض هذه تخص مستخدماً آخر."},
    "Un délégué est requis pendant l'absence.": {
        "en": "A delegate is required during the absence.",
        "de": "Während der Abwesenheit ist eine Vertretung erforderlich.",
        "es": "Se requiere un delegado durante la ausencia.",
        "zh": "缺勤期间需要指定代理人。", "ar": "المفوَّض مطلوب أثناء الغياب."},
    "Le délégué doit être un autre utilisateur.": {
        "en": "The delegate must be another user.", "de": "Die Vertretung muss ein anderer Benutzer sein.",
        "es": "El delegado debe ser otro usuario.", "zh": "代理人必须是其他用户。", "ar": "يجب أن يكون المفوَّض مستخدماً آخر."},
    "La fin d'absence doit être dans le futur.": {
        "en": "The absence end date must be in the future.",
        "de": "Das Abwesenheitsende muss in der Zukunft liegen.",
        "es": "El fin de la ausencia debe ser futuro.",
        "zh": "缺勤结束时间必须在未来。", "ar": "يجب أن يكون تاريخ نهاية الغياب مستقبلياً."},
    "Un auditeur (lecture seule) ne peut pas être délégué.": {
        "en": "An auditor (read-only) cannot be a delegate.",
        "de": "Ein Auditor (nur Lesen) kann keine Vertretung sein.",
        "es": "Un auditor (solo lectura) no puede ser delegado.",
        "zh": "审计员（只读）不能作为代理人。", "ar": "المدقق (قراءة فقط) لا يمكن أن يكون مفوَّضاً."},
    # Divers
    "Snapshot introuvable.": {
        "en": "Snapshot not found.", "de": "Snapshot nicht gefunden.",
        "es": "Instantánea no encontrada.", "zh": "未找到快照。", "ar": "اللقطة غير موجودة."},
    "Paire introuvable.": {
        "en": "Pair not found.", "de": "Paar nicht gefunden.",
        "es": "Par no encontrado.", "zh": "未找到配对。", "ar": "الزوج غير موجود."},
    "Paire déjà révoquée.": {
        "en": "Pair already revoked.", "de": "Paar bereits widerrufen.",
        "es": "Par ya revocado.", "zh": "配对已撤销。", "ar": "الزوج ملغى مسبقاً."},
    "Configuration importée.": {
        "en": "Configuration imported.", "de": "Konfiguration importiert.",
        "es": "Configuración importada.", "zh": "配置已导入。", "ar": "تم استيراد الإعدادات."},
    "Fichier déjà importé (même empreinte SHA-256) : rien n'a été réimporté.": {
        "en": "File already imported (same SHA-256 fingerprint): nothing was re-imported.",
        "de": "Datei bereits importiert (gleicher SHA-256-Fingerabdruck): nichts wurde erneut importiert.",
        "es": "Archivo ya importado (misma huella SHA-256): no se ha vuelto a importar nada.",
        "zh": "文件已导入过（SHA-256 指纹相同）：未重新导入任何内容。",
        "ar": "الملف مستورد مسبقاً (البصمة SHA-256 نفسها): لم يُعَد استيراد أي شيء."},
    "Compte utilisateur mis à jour.": {
        "en": "User account updated.", "de": "Benutzerkonto aktualisiert.",
        "es": "Cuenta de usuario actualizada.", "zh": "用户账户已更新。", "ar": "تم تحديث حساب المستخدم."},
    # Décision et priorité d'alerte : le statut et la priorité se disent par
    # leur libellé (narrative.ALERT_STATUS_LABELS, database.ALERT_PRIORITY_LABELS).
    "Décision validée, alerte close — faux positif.": {
        "en": "Decision validated, alert closed — false positive.",
        "de": "Entscheidung bestätigt, Alarm geschlossen — Fehlalarm.",
        "es": "Decisión validada, alerta cerrada — falso positivo.",
        "zh": "决定已确认，警报已关闭——误报。", "ar": "تمت المصادقة على القرار وأُغلق التنبيه — إيجابي كاذب."},
    "Décision validée, alerte close — vrai positif confirmé.": {
        "en": "Decision validated, alert closed — confirmed true positive.",
        "de": "Entscheidung bestätigt, Alarm geschlossen — bestätigter echter Treffer.",
        "es": "Decisión validada, alerta cerrada — verdadero positivo confirmado.",
        "zh": "决定已确认，警报已关闭——确认真阳性。", "ar": "تمت المصادقة على القرار وأُغلق التنبيه — إيجابي حقيقي مؤكد."},
    "Alerte déjà close — faux positif.": {
        "en": "Alert already closed — false positive.", "de": "Alarm bereits geschlossen — Fehlalarm.",
        "es": "Alerta ya cerrada — falso positivo.", "zh": "警报已关闭——误报。", "ar": "التنبيه مغلق مسبقاً — إيجابي كاذب."},
    "Alerte déjà close — vrai positif confirmé.": {
        "en": "Alert already closed — confirmed true positive.", "de": "Alarm bereits geschlossen — bestätigter echter Treffer.",
        "es": "Alerta ya cerrada — verdadero positivo confirmado.", "zh": "警报已关闭——确认真阳性。", "ar": "التنبيه مغلق مسبقاً — إيجابي حقيقي مؤكد."},
    "Alerte déjà close par règle anti-faux positif.": {
        "en": "Alert already closed by an anti-false-positive rule.", "de": "Alarm bereits durch eine Anti-Fehlalarm-Regel geschlossen.",
        "es": "Alerta ya cerrada por una regla anti falsos positivos.", "zh": "警报已被防误报规则关闭。", "ar": "التنبيه مغلق مسبقاً بقاعدة مكافحة الإنذارات الكاذبة."},
    "Priorité passée à basse.": {
        "en": "Priority set to low.", "de": "Priorität auf niedrig gesetzt.",
        "es": "Prioridad cambiada a baja.", "zh": "优先级已改为低。", "ar": "غُيّرت الأولوية إلى منخفضة."},
    "Priorité passée à moyenne.": {
        "en": "Priority set to medium.", "de": "Priorität auf mittel gesetzt.",
        "es": "Prioridad cambiada a media.", "zh": "优先级已改为中。", "ar": "غُيّرت الأولوية إلى متوسطة."},
    "Priorité passée à haute.": {
        "en": "Priority set to high.", "de": "Priorität auf hoch gesetzt.",
        "es": "Prioridad cambiada a alta.", "zh": "优先级已改为高。", "ar": "غُيّرت الأولوية إلى عالية."},
    "Priorité passée à critique.": {
        "en": "Priority set to critical.", "de": "Priorität auf kritisch gesetzt.",
        "es": "Prioridad cambiada a crítica.", "zh": "优先级已改为紧急。", "ar": "غُيّرت الأولوية إلى حرجة."},
    # Transmission du projet de declaration de soupcon (TRACFIN)
    "Alerte introuvable.": {
        "en": "Alert not found.", "de": "Alarm nicht gefunden.",
        "es": "Alerta no encontrada.", "zh": "未找到警报。", "ar": "التنبيه غير موجود."},
    "Aucun correspondant TRACFIN configuré (config.yaml, institution.correspondent_email).": {
        "en": "No TRACFIN correspondent configured (config.yaml, institution.correspondent_email).",
        "de": "Kein TRACFIN-Ansprechpartner konfiguriert (config.yaml, institution.correspondent_email).",
        "es": "Ningún corresponsal TRACFIN configurado (config.yaml, institution.correspondent_email).",
        "zh": "未配置 TRACFIN 联系人（config.yaml，institution.correspondent_email）。",
        "ar": "لا يوجد مراسل TRACFIN مُعرَّف (config.yaml، institution.correspondent_email)."},
    "Envoi impossible : SMTP non configuré (variable d'environnement SMTP_HOST absente).": {
        "en": "Cannot send: SMTP is not configured (SMTP_HOST environment variable missing).",
        "de": "Senden nicht möglich: SMTP ist nicht konfiguriert (Umgebungsvariable SMTP_HOST fehlt).",
        "es": "Envío imposible: SMTP no configurado (falta la variable de entorno SMTP_HOST).",
        "zh": "无法发送：未配置 SMTP（缺少环境变量 SMTP_HOST）。",
        "ar": "تعذّر الإرسال: SMTP غير مُعدّ (متغير البيئة SMTP_HOST مفقود)."},
    # Reglages de gouvernance
    "quality_min_score_pct doit être entre 0 et 100.": {
        "en": "quality_min_score_pct must be between 0 and 100.",
        "de": "quality_min_score_pct muss zwischen 0 und 100 liegen.",
        "es": "quality_min_score_pct debe estar entre 0 y 100.",
        "zh": "quality_min_score_pct 必须介于 0 和 100 之间。",
        "ar": "يجب أن تكون quality_min_score_pct بين 0 و100."},
}

# ---- Gabarits regex pour les messages a variables ----
TEMPLATES = [
    # Vrai positif sur une liste de gel : la validation annonce la mesure
    # ouverte au registre (api._message_de_mesure).
    (re.compile(r"^Décision validée, alerte close — vrai positif confirmé\. Mesure de gel #(\d+) ouverte : "
                r"déclaration à la DG Trésor due immédiatement \(Criblage → Registre des gels\)\.$"), {
        "en": "Decision validated, alert closed — confirmed true positive. Freeze measure #{0} opened: "
              "the declaration to the DG Trésor is due immediately (Screening → Freeze register).",
        "de": "Entscheidung bestätigt, Alarm geschlossen — bestätigter echter Treffer. Einfrierungsmaßnahme #{0} "
              "eröffnet: die Meldung an die DG Trésor ist sofort fällig (Screening → Register der Einfrierungen).",
        "es": "Decisión validada, alerta cerrada — verdadero positivo confirmado. Medida de congelación #{0} "
              "abierta: la declaración a la DG Trésor es exigible de inmediato (Cribado → Registro de congelaciones).",
        "zh": "决定已确认，警报已关闭——确认真阳性。已开立冻结措施 #{0}：须立即向 DG Trésor 申报（筛查 → 冻结登记册）。",
        "ar": "تمت المصادقة على القرار وأُغلق التنبيه — إيجابي حقيقي مؤكد. فُتح تدبير التجميد رقم {0}: "
              "التصريح للمديرية العامة للخزينة واجب فوراً (الفحص ← سجل التجميد)."}),
    # PPE confirmee : la validation annonce le dossier ouvert (api._message_de_dossier_ppe),
    # et dit quand le SI amont ne declare pas le client PPE
    (re.compile(r"^Décision validée, alerte close — vrai positif confirmé\. Dossier PPE #(\d+) ouvert : "
                r"décision d'entrer ou de rester en relation à prendre par une personne habilitée "
                r"\(Criblage → Dossiers PPE\)\.$"), {
        "en": "Decision validated, alert closed — confirmed true positive. PEP case #{0} opened: the "
              "decision to enter into or maintain the relationship is to be taken by an authorised "
              "person (Screening → PEP cases).",
        "de": "Entscheidung bestätigt, Alarm geschlossen — bestätigter echter Treffer. PEP-Akte #{0} "
              "eröffnet: die Entscheidung über Aufnahme oder Fortführung der Beziehung trifft eine "
              "befugte Person (Screening → PEP-Akten).",
        "es": "Decisión validada, alerta cerrada — verdadero positivo confirmado. Expediente PEP #{0} "
              "abierto: la decisión de iniciar o mantener la relación corresponde a una persona "
              "habilitada (Cribado → Expedientes PEP).",
        "zh": "决定已确认，警报已关闭——确认真阳性。已开立 PEP 档案 #{0}：建立或维持业务关系的决定须由获授权人员作出（筛查 → PEP 档案）。",
        "ar": "تمت المصادقة على القرار وأُغلق التنبيه — إيجابي حقيقي مؤكد. فُتح ملف الشخص المعرّض سياسياً رقم {0}: "
              "يعود قرار إقامة العلاقة أو مواصلتها إلى شخص مخوَّل (الفحص ← ملفات الأشخاص المعرّضين سياسياً)."}),
    (re.compile(r"^Décision validée, alerte close — vrai positif confirmé\. Dossier PPE #(\d+) ouvert : "
                r"décision d'entrer ou de rester en relation à prendre par une personne habilitée "
                r"\(Criblage → Dossiers PPE\)\. PPE non déclarée par le SI amont : la classification "
                r"du client y est à corriger\.$"), {
        "en": "Decision validated, alert closed — confirmed true positive. PEP case #{0} opened: the "
              "decision to enter into or maintain the relationship is to be taken by an authorised "
              "person (Screening → PEP cases). PEP not declared by the upstream system: the client's "
              "classification must be corrected there.",
        "de": "Entscheidung bestätigt, Alarm geschlossen — bestätigter echter Treffer. PEP-Akte #{0} "
              "eröffnet: die Entscheidung über Aufnahme oder Fortführung der Beziehung trifft eine "
              "befugte Person (Screening → PEP-Akten). Vom vorgelagerten System nicht als PEP "
              "gemeldet: die Einstufung des Kunden ist dort zu korrigieren.",
        "es": "Decisión validada, alerta cerrada — verdadero positivo confirmado. Expediente PEP #{0} "
              "abierto: la decisión de iniciar o mantener la relación corresponde a una persona "
              "habilitada (Cribado → Expedientes PEP). PEP no declarada por el sistema de origen: la "
              "clasificación del cliente debe corregirse allí.",
        "zh": "决定已确认，警报已关闭——确认真阳性。已开立 PEP 档案 #{0}：建立或维持业务关系的决定须由获授权人员作出（筛查 → PEP 档案）。上游系统未申报为 PEP：须在上游系统中更正客户分类。",
        "ar": "تمت المصادقة على القرار وأُغلق التنبيه — إيجابي حقيقي مؤكد. فُتح ملف الشخص المعرّض سياسياً رقم {0}: "
              "يعود قرار إقامة العلاقة أو مواصلتها إلى شخص مخوَّل (الفحص ← ملفات الأشخاص المعرّضين سياسياً). "
              "لم يصرّح النظام المصدر بأنه شخص معرّض سياسياً: يجب تصحيح تصنيف العميل هناك."}),
    (re.compile(r"^Compte temporairement verrouillé après trop d'échecs\. Réessayez dans (\d+) minute\(s\)\.$"), {
        "en": "Account temporarily locked after too many failures. Try again in {0} minute(s).",
        "de": "Konto nach zu vielen Fehlversuchen vorübergehend gesperrt. Erneut versuchen in {0} Minute(n).",
        "es": "Cuenta bloqueada temporalmente tras demasiados fallos. Reinténtelo en {0} minuto(s).",
        "zh": "失败次数过多，账户暂时锁定。请在 {0} 分钟后重试。",
        "ar": "الحساب مقفل مؤقتاً بعد محاولات فاشلة كثيرة. حاول بعد {0} دقيقة."}),
    (re.compile(r"^Mot de passe trop faible : il faut (.+)\.$"), {
        "en": "Password too weak: it needs {0}.",
        "de": "Passwort zu schwach: erforderlich ist {0}.",
        "es": "Contraseña demasiado débil: se necesita {0}.",
        "zh": "密码太弱：需要 {0}。",
        "ar": "كلمة المرور ضعيفة جداً: يلزم {0}."}),
    (re.compile(r"^Alerte assignée à (\S+)\.$"), {
        "en": "Alert assigned to {0}.", "de": "Alarm zugewiesen an {0}.",
        "es": "Alerta asignada a {0}.", "zh": "警报已指派给 {0}。", "ar": "أسند التنبيه إلى {0}."}),
    (re.compile(r"^(\d+) alerte\(s\) mise\(s\) à jour, (\d+) ignorée\(s\)\.$"), {
        "en": "{0} alert(s) updated, {1} skipped.",
        "de": "{0} Alarm(e) aktualisiert, {1} übersprungen.",
        "es": "{0} alerta(s) actualizada(s), {1} ignorada(s).",
        "zh": "已更新 {0} 条警报，跳过 {1} 条。", "ar": "حُدّث {0} تنبيه وتم تجاهل {1}."}),
    # File de l'analyste : proposition en masse et « Prendre la suivante »
    (re.compile(r"^(\d+) décision\(s\) proposée\(s\), chacune en attente de sa validation 4-yeux ; "
                r"(\d+) alerte\(s\) écartée\(s\)\.$"), {
        "en": "{0} decision(s) proposed, each awaiting its own four-eyes validation; {1} alert(s) set aside.",
        "de": "{0} Entscheidung(en) vorgeschlagen, jede wartet auf ihre eigene Vier-Augen-Freigabe; {1} Alarm(e) ausgelassen.",
        "es": "{0} decisión(es) propuesta(s), cada una pendiente de su validación a cuatro ojos; {1} alerta(s) descartada(s).",
        "zh": "已提议 {0} 项决定，每项都等待各自的四眼复核；{1} 条警报被排除。",
        "ar": "اقتُرح {0} قرار، كل منها بانتظار تحقق الأربع أعين الخاص به؛ استُبعد {1} تنبيه."}),
    (re.compile(r"^(\d+) alerte\(s\) clôturée\(s\) \(validation 4-yeux désactivée\) ; "
                r"(\d+) alerte\(s\) écartée\(s\)\.$"), {
        "en": "{0} alert(s) closed (four-eyes validation disabled); {1} alert(s) set aside.",
        "de": "{0} Alarm(e) geschlossen (Vier-Augen-Freigabe deaktiviert); {1} Alarm(e) ausgelassen.",
        "es": "{0} alerta(s) cerrada(s) (validación a cuatro ojos desactivada); {1} alerta(s) descartada(s).",
        "zh": "已关闭 {0} 条警报（四眼复核已停用）；{1} 条警报被排除。",
        "ar": "أُغلق {0} تنبيه (تحقق الأربع أعين معطّل)؛ استُبعد {1} تنبيه."}),
    (re.compile(r"^Vous proposez « faux positif » pour (\d+) alerte\(s\) d'une liste de gel \(périmètre "
                r"SANCTION\)\. Rien n'est clos : chaque proposition attendra sa propre validation 4-yeux\. "
                r"Mais une correspondance de gel manquée est constatable à l'audit et sanctionnable : "
                r"confirmez que chacune a été instruite individuellement\.$"), {
        "en": "You are proposing “false positive” for {0} alert(s) on a freeze list (SANCTION perimeter). "
              "Nothing is closed: each proposal will await its own four-eyes validation. But a missed freeze "
              "match is visible at audit and sanctionable: confirm that each one was investigated individually.",
        "de": "Sie schlagen „Fehlalarm“ für {0} Alarm(e) einer Einfrierungsliste vor (Perimeter SANCTION). "
              "Nichts wird geschlossen: jeder Vorschlag wartet auf seine eigene Vier-Augen-Freigabe. Ein "
              "übersehener Treffer ist jedoch bei der Prüfung feststellbar und sanktionierbar: bestätigen Sie, "
              "dass jeder einzeln bearbeitet wurde.",
        "es": "Propone «falso positivo» para {0} alerta(s) de una lista de congelación (perímetro SANCTION). "
              "Nada se cierra: cada propuesta esperará su propia validación a cuatro ojos. Pero una "
              "coincidencia de congelación omitida es constatable en auditoría y sancionable: confirme que "
              "cada una se instruyó individualmente.",
        "zh": "您正在为冻结名单（SANCTION 范围）上的 {0} 条警报提议“误报”。不会关闭任何警报："
              "每项提议都将等待各自的四眼复核。但漏掉冻结命中会在审计中被发现并可能受罚：请确认每条都已单独调查。",
        "ar": "أنت تقترح «إيجابي كاذب» لـ {0} تنبيه على قائمة تجميد (نطاق SANCTION). لا يُغلق شيء: "
              "سينتظر كل اقتراح تحقق الأربع أعين الخاص به. لكن التطابق الفائت مع قائمة تجميد يُكتشف عند "
              "التدقيق ويُعاقب عليه: أكّد أن كل تنبيه قد حُقق فيه على حدة."}),
    (re.compile(r"^Alerte #(\d+) prise en charge : la plus urgente des alertes non assignées\.$"), {
        "en": "Alert #{0} taken: the most urgent of the unassigned alerts.",
        "de": "Alarm #{0} übernommen: der dringendste der nicht zugewiesenen Alarme.",
        "es": "Alerta #{0} asumida: la más urgente de las alertas sin asignar.",
        "zh": "已领取警报 #{0}：未指派警报中最紧急的一条。",
        "ar": "أُخذ التنبيه رقم {0}: الأكثر إلحاحًا بين التنبيهات غير المسندة."}),
    (re.compile(r"^Motif réservé à un vrai positif : « (.+) »\. Il ne peut pas motiver un faux positif\.$"), {
        "en": "Reason reserved for a true positive: “{0}”. It cannot justify a false positive.",
        "de": "Begründung für einen echten Treffer reserviert: „{0}“. Sie kann keinen Fehlalarm begründen.",
        "es": "Motivo reservado a un verdadero positivo: «{0}». No puede motivar un falso positivo.",
        "zh": "该理由仅适用于真阳性：“{0}”。不能用于说明误报。",
        "ar": "سبب مخصص لإيجابي حقيقي: «{0}». لا يمكن أن يبرر إيجابيًا كاذبًا."}),
    (re.compile(r"^Motif réservé à un faux positif : « (.+) »\. Il ne peut pas motiver un vrai positif\.$"), {
        "en": "Reason reserved for a false positive: “{0}”. It cannot justify a true positive.",
        "de": "Begründung für einen Fehlalarm reserviert: „{0}“. Sie kann keinen echten Treffer begründen.",
        "es": "Motivo reservado a un falso positivo: «{0}». No puede motivar un verdadero positivo.",
        "zh": "该理由仅适用于误报：“{0}”。不能用于说明真阳性。",
        "ar": "سبب مخصص لإيجابي كاذب: «{0}». لا يمكن أن يبرر إيجابيًا حقيقيًا."}),
    (re.compile(r"^Vue « (.+) » sauvegardée\.$"), {
        "en": "View “{0}” saved.", "de": "Ansicht „{0}“ gespeichert.",
        "es": "Vista «{0}» guardada.", "zh": "视图“{0}”已保存。", "ar": "حُفظت طريقة العرض «{0}»."}),
    (re.compile(r"^Vue « (.+) » mise à jour\.$"), {
        "en": "View “{0}” updated.", "de": "Ansicht „{0}“ aktualisiert.",
        "es": "Vista «{0}» actualizada.", "zh": "视图“{0}”已更新。", "ar": "حُدّثت طريقة العرض «{0}»."}),
    (re.compile(r"^Vue « (.+) » supprimée\.$"), {
        "en": "View “{0}” deleted.", "de": "Ansicht „{0}“ gelöscht.",
        "es": "Vista «{0}» eliminada.", "zh": "视图“{0}”已删除。", "ar": "حُذفت طريقة العرض «{0}»."}),
    (re.compile(r"^Utilisateur '(.+)' créé avec succès\.$"), {
        "en": "User '{0}' created successfully.", "de": "Benutzer '{0}' erfolgreich angelegt.",
        "es": "Usuario '{0}' creado con éxito.", "zh": "用户“{0}”创建成功。", "ar": "تم إنشاء المستخدم '{0}' بنجاح."}),
    (re.compile(r"^Utilisateur '(.+)' supprimé avec succès\.$"), {
        "en": "User '{0}' deleted successfully.", "de": "Benutzer '{0}' erfolgreich gelöscht.",
        "es": "Usuario '{0}' eliminado con éxito.", "zh": "用户“{0}”删除成功。", "ar": "تم حذف المستخدم '{0}' بنجاح."}),
]


def resolve_lang(accept_language: Optional[str]) -> str:
    """Meilleure langue supportee d'un en-tete Accept-Language (defaut : fr)."""
    if not accept_language:
        return "fr"
    best_lang, best_q = "fr", 0.0
    for part in accept_language.split(","):
        piece = part.strip()
        if not piece:
            continue
        lang_tag, _, q_part = piece.partition(";")
        code = lang_tag.strip().lower().split("-")[0]
        try:
            quality = float(q_part.strip()[2:]) if q_part.strip().startswith("q=") else 1.0
        except ValueError:
            quality = 0.0
        if code in SUPPORTED_LANGS and quality > best_q:
            best_lang, best_q = code, quality
    return best_lang


def translate_message(text: str, lang: str) -> Optional[str]:
    """Traduction d'un message : catalogue exact puis gabarits. None = inconnu."""
    if not isinstance(text, str) or lang == "fr":
        return None
    entry = MESSAGES.get(text)
    if entry and entry.get(lang):
        return entry[lang]
    for pattern, templates in TEMPLATES:
        match = pattern.match(text)
        if match and templates.get(lang):
            return templates[lang].format(*match.groups())
    return None


def translate_payload(data: Any, lang: str) -> bool:
    """Traduit en place les champs message/detail d'une charge JSON.
    Retourne True si au moins un champ a change."""
    changed = False
    if isinstance(data, dict):
        for field in ("detail", "message"):
            value = data.get(field)
            if isinstance(value, str):
                out = translate_message(value, lang)
                if out is not None:
                    data[field] = out
                    changed = True
            elif isinstance(value, dict):
                if translate_payload(value, lang):
                    changed = True
    return changed
