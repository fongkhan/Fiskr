import csv
import random
from faker import Faker

# Initialisation de Faker avec une localisation bilingue pour un jeu de données réaliste
fake = Faker(['fr_FR', 'en_US'])

OUTPUT_FILE = "clients_test.csv"
TOTAL_RECORDS = 10000

print(f"Génération de {TOTAL_RECORDS} lignes de test client...")

# Définition des entêtes strictes alignées avec le Schéma Client du DAT
headers = [
    "client_id", "client_type", "client_first_name", "client_last_name", 
    "client_maiden_name", "client_company_name", "client_dob", "client_gender", 
    "client_is_deceased", "nationality", "residence", "birth_country", "registration_country"
]

with open(OUTPUT_FILE, mode="w", newline="", encoding="utf-8") as file:
    writer = csv.writer(file)
    writer.writerow(headers)
    
    # -------------------------------------------------------------------------
    # SCÉNARIOS DE TEST CRITIQUES (Injectés manuellement au début du fichier)
    # -------------------------------------------------------------------------
    scenarios = [
        # Cas 1 : Test de l'algorithme Token Sort (Inversion Prénom/Nom)
        ["CUST-SPEC-001", "PP", "PUTIN", "VLADIMIR", "", "", "1952-10-07", "M", "False", "RU", "RU", "RU", ""],
        
        # Cas 2 : Test de Jaro-Winkler & Levenshtein (Fautes de frappe sur Hans Müller)
        ["CUST-SPEC-002", "PP", "HANZ", "MUTLER", "", "", "1975-12-15", "M", "False", "DE", "FR", "DE", ""],
        
        # Cas 3 : Test du Hard Match sur le numéro LEI (Personne Morale - Priorité 1)
        ["CUST-SPEC-003", "PM", "", "", "", "SOCIETE GENERALE", "", "U", "False", "", "FR", "", "FR"],
        
        # Cas 4 : Test du match sur le Nom de Jeune Fille (Maiden Name)
        ["CUST-SPEC-004", "PP", "ALEXANDRA", "SMITH", "MULLER", "", "1988-04-23", "F", "False", "US", "US", "US", ""],
        
        # Cas 5 : Test du Malus Intergénérationnel (Même nom mais écart d'âge > 5 ans)
        ["CUST-SPEC-005", "PP", "VLADIMIR", "PUTIN", "", "", "1995-10-07", "M", "False", "RU", "FR", "RU", ""]
    ]
    
    for row in scenarios:
        writer.writerow(row)
        
    # -------------------------------------------------------------------------
    # GÉNÉRATION DE MASSE (Données aléatoires pour simuler la production)
    # -------------------------------------------------------------------------
    for i in range(1, TOTAL_RECORDS - len(scenarios) + 1):
        client_id = f"CUST-BATCH-{i:06d}"
        client_type = random.choice(["PP", "PP", "PP", "PM"]) # 75% de PP, 25% de PM
        
        # Initialisation des colonnes vides
        first_name = last_name = maiden_name = company_name = dob = ""
        gender = "U"
        is_deceased = "False"
        
        # Pays par défaut (Codes ISO2)
        nat = fake.country_code(representation="alpha-2")
        res = random.choice([nat, "FR", "FR", fake.country_code(representation="alpha-2")])
        birth_c = nat
        reg_c = ""
        
        if client_type == "PP":
            # Personne Physique
            gender = random.choice(["M", "F"])
            if gender == "M":
                first_name = fake.first_name_male().upper()
                last_name = fake.last_name().upper()
            else:
                first_name = fake.first_name_female().upper()
                last_name = fake.last_name().upper()
                # 20% de chance d'avoir un nom de jeune fille pour les femmes
                if random.random() < 0.2:
                    maiden_name = fake.last_name().upper()
            
            dob = fake.date_of_birth(minimum_age=18, maximum_age=90).strftime("%Y-%m-%d")
            # 1% de chance que le client soit marqué décédé (pour tester le statut vital)
            if random.random() < 0.01:
                is_deceased = "True"
                
        else:
            # Personne Morale
            company_name = fake.company().upper()
            reg_c = res
            # Génération d'un faux numéro LEI structuré (20 caractères) pour la validation Rule_M07
            if random.random() < 0.8:
                client_id_lei = fake.bothify(text="####################").upper()
            else:
                client_id_lei = ""
                
        # Assemblage de la ligne conforme au schéma
        writer.writerow([
            client_id, client_type, first_name, last_name, maiden_name, 
            company_name, dob, gender, is_deceased, nat, res, birth_c, reg_c
        ])

print(f"Fichier '{OUTPUT_FILE}' généré avec succès ! Prêt pour le Batch Spark.")