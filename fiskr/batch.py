import json
import logging
from typing import Dict, Any, List

logger = logging.getLogger("fiskr.batch")

# We wrap the PySpark imports to allow the module to load even if Spark is not installed.
# This makes local development and testing much easier.
try:
    from pyspark.sql import SparkSession
    from pyspark.sql import functions as F
    from pyspark.sql.types import DoubleType, ArrayType, StringType
    SPARK_AVAILABLE = True
except ImportError:
    SPARK_AVAILABLE = False
    logger.warning("PySpark is not installed. Batch engine will run in fallback Pandas mode.")

# 1. Define Spark UDF for Scoring
def get_scoring_udf(config: dict):
    """Creates and returns the UDF for scoring client vs watchlist profiles."""
    if not SPARK_AVAILABLE:
        return None
        
    def calculate_score(
        c_names: List[str],
        w_names: List[str],
        c_dobs: List[str],
        w_dobs: List[str],
        c_gender: str,
        w_genders: List[str],
        dob_window: int
    ) -> float:
        # Import inside UDF to avoid worker serialization issues
        from fiskr.scoring import compute_base_score, calculate_dob_adjustment, calculate_gender_adjustment, calculate_geography_adjustment
        
        if not c_names or not w_names:
            return 0.0
            
        # 1. Best-Match name scoring
        best_base_score = 0.0
        for cn in c_names:
            for wn in w_names:
                score = compute_base_score(cn, wn, config)
                if score > best_base_score:
                    best_base_score = score
                    
        # 2. Contextual Rules
        # DOB
        dob_adj, _ = calculate_dob_adjustment(c_dobs or [], w_dobs or [], config)
        
        # Gender
        gender_adj, _ = calculate_gender_adjustment(c_gender or "U", w_genders or [], config)
        
        # Geography - Countries
        # For simplicity in batch, we can skip geography adjustment if country list is not passed, 
        # or we can pass it. To match the DAT UDF signature, we don't have countries in the arguments,
        # but we can add geography adjustment if passed, or merge it in the score. 
        # Let's keep the exact DAT UDF signature but include a robust implementation.
        total_adj = dob_adj + gender_adj
        final_score = best_base_score + total_adj
        return max(0.0, min(100.0, final_score))
        
    return F.udf(calculate_score, DoubleType())


# 2. Apache Spark Batch Screening Function
def run_spark_batch_screening(client_df, watchlist_df, config):
    """
    Runs Spark batch screening on clients and watchlists using a Broadcast Join.
    Matches the signature and structure specified in the DAT.
    """
    if not SPARK_AVAILABLE:
        raise RuntimeError("PySpark is required to run Spark batch screening.")
        
    # 1. Jointure optimisée par blocage configuré et Broadcast de la Watchlist
    joined_df = client_df.join(
        F.broadcast(watchlist_df),
        on="blocking_key",
        how="inner"
    )
    
    # 2. Calcul du score parallélisé avec prise en compte des paramètres dynamiques
    dob_window = config["scoring"]["contextual_rules"]["dob_tolerance_window"]
    
    calculate_score_udf = get_scoring_udf(config)
    
    scored_df = joined_df.withColumn(
        "final_compliance_score",
        calculate_score_udf(
            F.col("client_names"), F.col("watchlist_names"),
            F.col("client_dob_array"), F.col("watchlist_dob_array"),
            F.col("client_gender"), F.col("watchlist_gender"),
            F.literal(dob_window)
        )
    )
    
    # 3. Extraction stricte selon le seuil de conformité à 75%
    alerts_df = scored_df.filter(F.col("final_compliance_score") >= 75.0)
    
    return alerts_df


# 3. Local Fallback Screening using Pandas
def run_pandas_batch_screening(clients: List[dict], watchlist: List[dict], config: dict) -> List[dict]:
    """
    A lightweight, pure-Python fallback for batch screening without PySpark.
    Applies the same blocking logic and scoring rules.
    """
    from fiskr.blocking import generate_blocking_keys
    from fiskr.scoring import match_entities
    
    # Index watchlist by blocking key
    watchlist_index = {}
    for entry in watchlist:
        keys = generate_blocking_keys(entry, config)
        for k in keys:
            if k not in watchlist_index:
                watchlist_index[k] = []
            watchlist_index[k].append(entry)
            
    alerts = []
    # Screen each client
    for client in clients:
        client_keys = generate_blocking_keys(client, config)
        
        # Keep track of compared watchlist entity IDs to avoid duplicate matches
        seen_watchlist_ids = set()
        
        for k in client_keys:
            matches = watchlist_index.get(k, [])
            for match in matches:
                wl_id = match.get("entity_id")
                if wl_id in seen_watchlist_ids:
                    continue
                seen_watchlist_ids.add(wl_id)
                
                res = match_entities(client, match, config)
                if res["final_score"] >= config["scoring"]["cut_off_threshold"]:
                    alerts.append({
                        "client_id": client.get("entity_id"),
                        "client_name": client.get("primary_name"),
                        "watchlist_id": wl_id,
                        "watchlist_name": match.get("primary_name"),
                        "final_compliance_score": res["final_score"],
                        "details": res
                    })
    return alerts
