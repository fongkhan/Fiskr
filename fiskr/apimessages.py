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
    "Action en masse inconnue (assign ou priority).": {
        "en": "Unknown bulk action (assign or priority).",
        "de": "Unbekannte Massenaktion (assign oder priority).",
        "es": "Acción masiva desconocida (assign o priority).",
        "zh": "未知的批量操作（assign 或 priority）。", "ar": "إجراء جماعي غير معروف (assign أو priority)."},
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
}

# ---- Gabarits regex pour les messages a variables ----
TEMPLATES = [
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
    (re.compile(r"^Vue « (.+) » sauvegardée\.$"), {
        "en": "View “{0}” saved.", "de": "Ansicht „{0}“ gespeichert.",
        "es": "Vista «{0}» guardada.", "zh": "视图“{0}”已保存。", "ar": "حُفظت طريقة العرض «{0}»."}),
    (re.compile(r"^Vue « (.+) » mise à jour\.$"), {
        "en": "View “{0}” updated.", "de": "Ansicht „{0}“ aktualisiert.",
        "es": "Vista «{0}» actualizada.", "zh": "视图“{0}”已更新。", "ar": "حُدّثت طريقة العرض «{0}»."}),
    (re.compile(r"^Vue « (.+) » supprimée\.$"), {
        "en": "View “{0}” deleted.", "de": "Ansicht „{0}“ gelöscht.",
        "es": "Vista «{0}» eliminada.", "zh": "视图“{0}”已删除。", "ar": "حُذفت طريقة العرض «{0}»."}),
    (re.compile(r"^Priorité passée à (\S+)\.$"), {
        "en": "Priority set to {0}.", "de": "Priorität gesetzt auf {0}.",
        "es": "Prioridad cambiada a {0}.", "zh": "优先级已改为 {0}。", "ar": "غُيّرت الأولوية إلى {0}."}),
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
