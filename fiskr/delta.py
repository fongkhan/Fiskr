from typing import List, Dict, Any, Tuple
import json
from fiskr.database import compute_checksum

def flatten_dict(d: dict, prefix: str = "") -> dict:
    """Recursively flattens a nested dictionary into dot-notation keys."""
    if not isinstance(d, dict):
        return {}
    items = {}
    for k, v in d.items():
        new_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.update(flatten_dict(v, new_key))
        else:
            items[new_key] = v
    return items

def find_differences(old_ent: dict, new_ent: dict) -> Tuple[List[str], dict, dict]:
    """
    Compares two entity dicts.
    Returns:
        changes_detected: list of strings (e.g. ["dates_of_birth", "countries.residence"])
        before: dict of changed keys
        after: dict of changed keys
    """
    changes_detected = []
    before = {}
    after = {}
    
    exclude_keys = {"id", "snapshot_id", "entity_checksum"}
    
    # 1. First compare root keys (non-dict)
    root_keys = (set(old_ent.keys()) | set(new_ent.keys())) - exclude_keys
    
    for k in root_keys:
        val_old = old_ent.get(k)
        val_new = new_ent.get(k)
        
        if isinstance(val_old, dict) or isinstance(val_new, dict):
            # Nested comparison
            flat_old = flatten_dict(val_old or {})
            flat_new = flatten_dict(val_new or {})
            all_flat_keys = set(flat_old.keys()) | set(flat_new.keys())
            
            sub_changes = []
            for fk in all_flat_keys:
                if flat_old.get(fk) != flat_new.get(fk):
                    sub_changes.append(fk)
                    
            if sub_changes:
                # Add nested paths to changes_detected
                for sc in sub_changes:
                    changes_detected.append(f"{k}.{sc}")
                before[k] = val_old
                after[k] = val_new
        else:
            # Flat comparison
            if val_old != val_new:
                changes_detected.append(k)
                before[k] = val_old
                after[k] = val_new
                
    return sorted(changes_detected), before, after

def calculate_delta(
    old_entities: List[Dict[str, Any]],
    new_entities: List[Dict[str, Any]],
    key_column: str
) -> Dict[str, Any]:
    """
    Compares two list of entities and returns the delta report.
    Matches entity list format specified in DAT Section 8.4.
    """
    old_map = {ent.get(key_column): ent for ent in old_entities if ent.get(key_column)}
    new_map = {ent.get(key_column): ent for ent in new_entities if ent.get(key_column)}
    
    old_ids = set(old_map.keys())
    new_ids = set(new_map.keys())
    
    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    common_ids = old_ids.intersection(new_ids)
    
    added = []
    removed = []
    modified = []
    
    # ADDED
    for i in sorted(added_ids):
        ent = new_map[i]
        name = ent.get("primary_name") or ent.get("client_company_name") or ent.get("client_last_name") or ""
        etype = ent.get("entity_type") or ent.get("client_type") or "I"
        added.append({"id": i, "primary_name": name, "type": etype})
        
    # REMOVED
    for i in sorted(removed_ids):
        ent = old_map[i]
        name = ent.get("primary_name") or ent.get("client_company_name") or ent.get("client_last_name") or ""
        etype = ent.get("entity_type") or ent.get("client_type") or "I"
        removed.append({"id": i, "primary_name": name, "type": etype})
        
    # MODIFIED (Using checksum comparisons)
    for i in sorted(common_ids):
        old_ent = old_map[i]
        new_ent = new_map[i]
        
        # Determine checksums
        old_chk = old_ent.get("entity_checksum")
        new_chk = new_ent.get("entity_checksum")
        
        if not old_chk:
            old_chk = compute_checksum(old_ent)
        if not new_chk:
            new_chk = compute_checksum(new_ent)
            
        if old_chk != new_chk:
            changes, before, after = find_differences(old_ent, new_ent)
            if changes:
                name = new_ent.get("primary_name") or new_ent.get("client_company_name") or new_ent.get("client_last_name") or ""
                modified.append({
                    "id": i,
                    "primary_name": name,
                    "changes_detected": changes,
                    "before": before,
                    "after": after
                })
                
    return {
        "summary": {
            "added_count": len(added),
            "removed_count": len(removed),
            "modified_count": len(modified)
        },
        "details": {
            "added": added,
            "removed": removed,
            "modified": modified
        }
    }
