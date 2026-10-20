"""
Le point de depart de la conservation : la fin de la relation, pas la date de la ligne.

POURQUOI CE MODULE EXISTE
-------------------------
La purge de retention (fiskr/retention.py) comptait sa duree depuis la date de
la ligne — la decision d'une alerte, l'horodatage d'un criblage — et ne
regardait jamais si le client etait encore en relation. Regle a 1 825 jours,
elle detruisait donc le dossier d'une alerte confirmee decidee il y a 1 826
jours chez un client toujours en portefeuille. Le referentiel ne portait
d'ailleurs aucune date de fin de relation : la question ne pouvait pas meme se
poser.

Ce que la loi compte (textes relus le 20 octobre 2026) :

- CMF art. L.561-12 (redaction de l'ordonnance n° 2020-115) : « conservent
  pendant cinq ans a compter de la cloture de leurs comptes ou de la cessation
  de leurs relations avec eux les documents et informations, quel qu'en soit
  le support, relatifs a leurs relations d'affaires ou clients occasionnels,
  ainsi qu'aux mesures de vigilance mises en oeuvre. Elles conservent
  egalement [...] pendant cinq ans a compter de leur execution [...] les
  documents et informations relatifs aux operations faites par ceux-ci » ;
- directive (UE) 2015/849, art. 40 §1 a) et b) : « pendant cinq ans apres la
  fin de la relation d'affaires avec le client ou apres la date de la
  transaction conclue a titre occasionnel » — et, a l'issue, les donnees a
  caractere personnel sont EFFACEES : garder sans limite serait le defaut
  inverse ;
- reglement (UE) 2024/1624, art. 77 §3 (applicable au 10 juillet 2027) : cinq
  ans « a compter de la date de fin de la relation d'affaires, de la date de
  l'execution de la transaction a titre occasionnel, ou de la date de refus de
  nouer une relation d'affaires ».

Le plancher n'est donc pas une duree depuis la ligne : c'est une duree depuis
la FIN DE LA RELATION. Un client en relation — ou dont le referentiel ne dit
pas qu'il en est sorti — n'est jamais purge.

CINQ ANNEES CIVILES, PAS 5 x 365 JOURS
--------------------------------------
Toute fenetre de cinq ans contient au moins un 29 fevrier, et parfois deux
(du 1er mars 2023 au 1er mars 2028 : 1 827 jours). 1 825 jours, c'est donc
TOUJOURS moins que cinq ans : la limite se calcule ici en annees civiles, par
arithmetique de date, et le nombre de jours qu'affichent les reglages en est
DERIVE (la plus longue fenetre).

LE PROSPECT CRIBLE SANS FICHE CLIENT
------------------------------------
Un criblage temps reel (`/api/screen`, webhook) d'une personne qui n'a pas de
fiche au referentiel — prospect, partie d'un paiement (« TXN:… ») — est traite
comme une OPERATION OCCASIONNELLE : cinq ans a compter de la ligne elle-meme
(art. 40 : « date de la transaction conclue a titre occasionnel » ; art. 77 :
« date de refus de nouer une relation d'affaires »). Pas plus tot : c'est
souvent la seule trace du refus. Pas « jamais » : la directive impose
l'effacement a l'issue. Et la relation se lit AU MOMENT DE LA PURGE, pas a
l'ecriture de la ligne : un prospect devenu client sous le meme identifiant
suit le delai de sa relation, donc n'est pas purge tant qu'elle dure.
"""
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import case, exists, select

from fiskr.database import ClientEntity, Snapshot

DUREE_LEGALE_ANNEES = 5


def il_y_a(maintenant: datetime, annees: int = DUREE_LEGALE_ANNEES) -> datetime:
    """`maintenant` recule d'un nombre d'annees CIVILES.

    Un 29 fevrier sans 29 fevrier cinq ans plus tot recule au 28 : la limite
    la plus ancienne des deux, donc celle qui conserve un jour de plus."""
    try:
        return maintenant.replace(year=maintenant.year - annees)
    except ValueError:
        return maintenant.replace(year=maintenant.year - annees, day=28)


def limite_legale(maintenant: Optional[datetime] = None) -> datetime:
    """Avant cette date, le delai legal doit avoir commence a courir pour
    qu'une piece probante soit purgeable."""
    return il_y_a(maintenant or datetime.utcnow())


# Le plancher en JOURS, pour comparer une politique reglee en jours : la plus
# longue fenetre de cinq annees civiles (deux 29 fevrier). Derive du calendrier
# sur un cycle bissextile complet, jamais recopie : 1 827.
_CYCLE = datetime(2028, 1, 1)
DUREE_LEGALE_JOURS = max(
    (jour - il_y_a(jour)).days
    for jour in (_CYCLE + timedelta(days=i) for i in range(4 * 366)))


# ------------------------------------------------- la date de fin de relation

_FORMATS = ("%Y-%m-%d", "%d/%m/%Y")


def date_de_fin_de_relation(valeur: Any) -> Optional[str]:
    """
    La date de fin de relation sous la forme STOCKEE (AAAA-MM-JJ), ou None.

    Une seule lecture, pour l'import, l'apercu et le webhook : la colonne se
    compare en SQL comme une chaine, et seule la forme ISO s'y ordonne comme
    une date. Une valeur illisible rend None — l'appelant le DIT : ecrite telle
    quelle, « 20/10/2019 » se classerait avant « 2021-10-20 » et rendrait le
    client purgeable sur une comparaison de caracteres.
    """
    if valeur is None:
        return None
    if isinstance(valeur, datetime):
        return valeur.date().isoformat()
    if isinstance(valeur, date):
        return valeur.isoformat()
    texte = str(valeur).strip()
    if not texte:
        return None
    # Un horodatage complet (2021-03-31T00:00:00Z) : seule la date compte
    if len(texte) > 10 and texte[10] in "T ":
        texte = texte[:10]
    for forme in _FORMATS:
        try:
            return datetime.strptime(texte, forme).date().isoformat()
        except ValueError:
            continue
    return None


def illisible(brute: Any, lue: Optional[str]) -> bool:
    """Une valeur fournie que la lecture n'a pas su comprendre."""
    return lue is None and brute is not None and str(brute).strip() != ""


# ------------------------------------------------- pourquoi une piece reste

PURGEABLE = "purgeable"
EN_RELATION = "en_relation"
FIN_DE_RELATION_RECENTE = "fin_de_relation_recente"
OPERATION_OCCASIONNELLE = "operation_occasionnelle"

# Ce que l'ecran de retention dit d'une piece que la politique aurait purgee
# et que la loi garde. Ce n'est pas une alarme : un client en portefeuille
# garde son dossier, c'est le fonctionnement normal.
MOTIFS = {
    EN_RELATION: "client en relation : aucune date de fin au référentiel",
    FIN_DE_RELATION_RECENTE: "client sorti de relation : délai non écoulé depuis la fin de la relation",
    OPERATION_OCCASIONNELLE: "opération occasionnelle (sans fiche client) de moins de cinq ans",
}


def _fiches_en_production(colonne_client):
    return (select(ClientEntity.client_relationship_end)
            .join(Snapshot, Snapshot.snapshot_id == ClientEntity.snapshot_id)
            .where(ClientEntity.client_id == colonne_client,
                   Snapshot.file_type == "CLIENT_BASE", Snapshot.status == "READY"))


def motif(colonne_client, colonne_date, limite: datetime):
    """
    Expression SQL : pourquoi la piece (client `colonne_client`, datee
    `colonne_date`) est conservee a la `limite`, ou PURGEABLE.

    LA definition — la purge filtre sur PURGEABLE, l'ecran compte les autres
    valeurs : ce qui est annonce comme conserve est exactement ce que la purge
    epargne. Le delai court :
    - sans fiche au referentiel : depuis la piece (operation occasionnelle) ;
    - fiche SANS date de fin : il ne court pas — le client est en relation ;
    - fiche avec date de fin : depuis la plus tardive de la fin et de la
      piece (une piece posterieure a la fin a ses cinq ans a elle).
    La fiche lue est la plus recente en production, comme partout ailleurs
    (vue client, registre des gels). Un CASE plutot qu'un NOT : la date de fin
    est nullable, et une negation sur NULL vaut NULL — elle ferait disparaitre
    la piece du compte comme de la purge, sans un mot.
    """
    fiche = exists(_fiches_en_production(colonne_client))
    fin = (_fiches_en_production(colonne_client)
           .order_by(Snapshot.uploaded_at.desc(), ClientEntity.id.desc())
           .limit(1).scalar_subquery())
    jour = limite.date().isoformat()
    return case(
        (~fiche, case((colonne_date < limite, PURGEABLE), else_=OPERATION_OCCASIONNELLE)),
        (fin.is_(None), EN_RELATION),
        ((fin < jour) & (colonne_date < limite), PURGEABLE),
        else_=FIN_DE_RELATION_RECENTE,
    )
