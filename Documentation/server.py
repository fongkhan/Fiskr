import sys
import os
import json
import csv
import io
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import xml.etree.ElementTree as ET

DATA_FILE = "sdn_advanced.xml"
HOST = "0.0.0.0"
PORT = 8000

print("Initializing OFAC Data Server...")

last_comparison_result = None

class OFACDatabase:
    def __init__(self):
        self.references = {}
        self.ref_links = {}  # Stores cross-reference attributes: {RefType: {id: {attr: val, ...}}}
        self.profiles = {}
        self.search_index = []
        self.locations = {}
        self.location_area_codes = {}  # {location_id: area_code_id}
        self.sanctions_programs = set()
    
    def load(self, file_path):
        print(f"Loading data from {file_path}")
        context = ET.iterparse(file_path, events=('start', 'end'))
        self.references = {}
        self.ref_links = {}
        self.profiles = {}
        self.search_index = []
        self.locations = {}
        self.location_area_codes = {}
        
        in_ref = False
        in_locs = False
        count = 0
        
        for event, elem in context:
            tag = elem.tag.split('}')[-1]
            
            if event == 'start' and tag == 'ReferenceValueSets':
                in_ref = True
            
            if event == 'start' and tag == 'Locations':
                in_locs = True

            if event == 'end' and in_ref and tag == 'ReferenceValueSets':
                for value_set in list(elem):
                    vs_tag = value_set.tag.split('}')[-1]
                    base_tag = vs_tag.replace('Values', '')
                    if base_tag not in self.references:
                        self.references[base_tag] = {}
                    if base_tag not in self.ref_links:
                        self.ref_links[base_tag] = {}
                    for child in list(value_set):
                        if 'ID' in child.attrib:
                            id_val = child.attrib['ID']
                            if child.text and child.text.strip():
                                val = child.text.strip()
                            elif 'Description' in child.attrib:
                                val = child.attrib['Description']
                            else:
                                val = str(child.attrib)
                            self.references[base_tag][id_val] = val
                            # Store all extra attributes as cross-reference links
                            extra = {k: v for k, v in child.attrib.items() if k != 'ID'}
                            if child.text and child.text.strip():
                                extra['_text'] = child.text.strip()
                            if extra:
                                self.ref_links[base_tag][id_val] = extra
                elem.clear()
                in_ref = False

            if event == 'end' and in_locs and tag == 'Location':
                if 'ID' in elem.attrib:
                    id_val = elem.attrib['ID']
                    # Capture AreaCodeID attribute from Location element
                    ac_id = elem.attrib.get('AreaCodeID')
                    if ac_id:
                        self.location_area_codes[id_val] = ac_id
                    # Extract location details
                    loc_parts = []
                    ns = elem.tag.split('}')[0] + '}'
                    for p in elem.iter(f"{ns}Value"):
                        if p.text:
                            loc_parts.append(p.text.strip())
                    for p in elem.iter(f"{ns}LocationCountry"):
                        cid = p.attrib.get('CountryID')
                        if cid and cid in self.references.get('Country', {}):
                            loc_parts.append(self.references['Country'][cid])
                    self.locations[id_val] = ", ".join(loc_parts)
                elem.clear()

            if event == 'end' and in_locs and tag == 'Locations':
                in_locs = False
                self.references['Location'] = self.locations
                
            if event == 'end' and tag == 'DistinctParty':
                ns = elem.tag.split('}')[0] + '}'
                prof_elem = elem.find(f'{ns}Profile')
                if prof_elem is None:
                    elem.clear()
                    continue
                    
                pid = prof_elem.attrib.get('ID')
                if not pid:
                    elem.clear()
                    continue
                
                # Extract names for index
                names = []
                for np_val in prof_elem.iter(f"{ns}NamePartValue"):
                    if np_val.text:
                        names.append(np_val.text.strip())
                
                search_text = (" ".join(names) + " " + pid).lower()
                self.search_index.append((search_text, pid))
                
                prof_dict = self._elem_to_dict(prof_elem)
                
                # Extract Comment
                comment_elem = elem.find(f'{ns}Comment')
                if comment_elem is not None and comment_elem.text:
                    prof_dict['DistinctPartyComment'] = comment_elem.text.strip()
                
                self.profiles[pid] = prof_dict
                
                elem.clear()
                count += 1
                if count % 10000 == 0:
                    print(f"Loaded {count} profiles...")
                    
        print(f"Loaded total {count} profiles into memory.")
        
        # Second pass: parse SanctionsEntries and attach to profiles
        print("Loading SanctionsEntries...")
        try:
            context2 = ET.iterparse(file_path, events=('start', 'end'))
            in_sanctions = False
            se_count = 0
            for event, elem in context2:
                tag = elem.tag.split('}')[-1]
                if event == 'start' and tag == 'SanctionsEntries':
                    in_sanctions = True
                if event == 'end' and in_sanctions and tag == 'SanctionsEntry':
                    profile_id = elem.attrib.get('ProfileID', '')
                    if profile_id and profile_id in self.profiles:
                        ns = elem.tag.split('}')[0] + '}'
                        se_data = {}
                        se_data['ListID'] = elem.attrib.get('ListID', '')
                        
                        # Resolve ListID
                        list_ref = self.references.get('List', {})
                        if se_data['ListID'] in list_ref:
                            se_data['ListName'] = list_ref[se_data['ListID']]
                        
                        # EntryEvent
                        for ee in elem.iter(f'{ns}EntryEvent'):
                            ee_type_id = ee.attrib.get('EntryEventTypeID', '')
                            legal_id = ee.attrib.get('LegalBasisID', '')
                            ee_type_ref = self.references.get('EntryEventType', {})
                            legal_ref = self.references.get('LegalBasis', {})
                            se_data['EntryEventType'] = ee_type_ref.get(ee_type_id, ee_type_id)
                            se_data['LegalBasis'] = legal_ref.get(legal_id, legal_id)
                            
                            # Entry date
                            date_parts = []
                            for y in ee.iter(f'{ns}Year'):
                                if y.text: date_parts.append(y.text.strip())
                            for m in ee.iter(f'{ns}Month'):
                                if m.text: date_parts.insert(1, m.text.strip())
                            for d in ee.iter(f'{ns}Day'):
                                if d.text: date_parts.append(d.text.strip())
                            if date_parts:
                                se_data['EntryDate'] = "-".join(date_parts)
                            break  # take first EntryEvent
                        
                        # SanctionsMeasures
                        measures = []
                        sanctions_type_ref = self.references.get('SanctionsType', {})
                        for sm in elem.iter(f'{ns}SanctionsMeasure'):
                            st_id = sm.attrib.get('SanctionsTypeID', '')
                            st_name = sanctions_type_ref.get(st_id, st_id)
                            comment_el = sm.find(f'{ns}Comment')
                            comment_txt = comment_el.text.strip() if comment_el is not None and comment_el.text else ''
                            measures.append({'SanctionsType': st_name, 'Comment': comment_txt})
                        se_data['SanctionsMeasures'] = measures
                        
                        # Attach to profile
                        if 'SanctionsEntries' not in self.profiles[profile_id]:
                            self.profiles[profile_id]['SanctionsEntries'] = []
                        self.profiles[profile_id]['SanctionsEntries'].append(se_data)
                        se_count += 1
                    elem.clear()
                if event == 'end' and tag == 'SanctionsEntries':
                    break
            print(f"Linked {se_count} sanctions entries to profiles.")
        except Exception as e:
            print(f"Warning: Could not parse SanctionsEntries: {e}")
        
        # Rebuild search index to include sanctions programs and collect unique program names
        print("Rebuilding search index with sanctions data...")
        self.sanctions_programs = set()
        new_index = []
        for search_text, pid in self.search_index:
            profile = self.profiles.get(pid, {})
            programs = []
            for se in profile.get('SanctionsEntries', []):
                for sm in se.get('SanctionsMeasures', []):
                    comment = sm.get('Comment', '')
                    if comment:
                        programs.append(comment)
                        self.sanctions_programs.add(comment)
            if programs:
                search_text = search_text + " " + " ".join(programs).lower()
            new_index.append((search_text, pid))
        self.search_index = new_index
        print(f"Search index rebuilt. Found {len(self.sanctions_programs)} unique sanctions programs.")

    def compare(self, other_file_path):
        """
        Compare the current database with another XML file.
        Returns a dictionary with added, removed, and modified profiles.
        """
        print(f"Comparing current database with {other_file_path}...")
        
        # We'll use a temporary dictionary for the other profiles
        other_profiles = {}
        
        # Pass 1: Profiles
        context = ET.iterparse(other_file_path, events=('start', 'end'))
        for event, elem in context:
            tag = elem.tag.split('}')[-1]
            if event == 'end' and tag == 'DistinctParty':
                ns = elem.tag.split('}')[0] + '}'
                prof_elem = elem.find(f'{ns}Profile')
                if prof_elem is not None:
                    pid = prof_elem.attrib.get('ID')
                    if pid:
                        prof_dict = self._elem_to_dict(prof_elem)
                        comment_elem = elem.find(f'{ns}Comment')
                        if comment_elem is not None and comment_elem.text:
                            prof_dict['DistinctPartyComment'] = comment_elem.text.strip()
                        other_profiles[pid] = prof_dict
                elem.clear()
        
        # Pass 2: SanctionsEntries
        try:
            context2 = ET.iterparse(other_file_path, events=('start', 'end'))
            in_sanctions = False
            for event, elem in context2:
                tag = elem.tag.split('}')[-1]
                if event == 'start' and tag == 'SanctionsEntries':
                    in_sanctions = True
                if event == 'end' and in_sanctions and tag == 'SanctionsEntry':
                    pid = elem.attrib.get('ProfileID', '')
                    if pid and pid in other_profiles:
                        ns = elem.tag.split('}')[0] + '}'
                        se_data = {'ListID': elem.attrib.get('ListID', '')}
                        list_ref = self.references.get('List', {})
                        if se_data['ListID'] in list_ref:
                            se_data['ListName'] = list_ref[se_data['ListID']]
                        for ee in elem.iter(f'{ns}EntryEvent'):
                            ee_type_id = ee.attrib.get('EntryEventTypeID', '')
                            legal_id = ee.attrib.get('LegalBasisID', '')
                            ee_type_ref = self.references.get('EntryEventType', {})
                            legal_ref = self.references.get('LegalBasis', {})
                            se_data['EntryEventType'] = ee_type_ref.get(ee_type_id, ee_type_id)
                            se_data['LegalBasis'] = legal_ref.get(legal_id, legal_id)
                            date_parts = []
                            for y in ee.iter(f'{ns}Year'):
                                if y.text: date_parts.append(y.text.strip())
                            for m in ee.iter(f'{ns}Month'):
                                if m.text: date_parts.insert(1, m.text.strip())
                            for d in ee.iter(f'{ns}Day'):
                                if d.text: date_parts.append(d.text.strip())
                            if date_parts: se_data['EntryDate'] = "-".join(date_parts)
                            break
                        measures = []
                        st_ref = self.references.get('SanctionsType', {})
                        for sm in elem.iter(f'{ns}SanctionsMeasure'):
                            st_id = sm.attrib.get('SanctionsTypeID', '')
                            measures.append({'SanctionsType': st_ref.get(st_id, st_id), 
                                           'Comment': sm.find(f'{ns}Comment').text.strip() if sm.find(f'{ns}Comment') is not None and sm.find(f'{ns}Comment').text else ''})
                        se_data['SanctionsMeasures'] = measures
                        if 'SanctionsEntries' not in other_profiles[pid]:
                            other_profiles[pid]['SanctionsEntries'] = []
                        other_profiles[pid]['SanctionsEntries'].append(se_data)
                if event == 'end' and tag == 'SanctionsEntries':
                    break
        except Exception as e:
            print(f"Warning during comparison sanctions parse: {e}")

        added = []
        removed = []
        modified = []
        
        current_ids = set(self.profiles.keys())
        other_ids = set(other_profiles.keys())
        
        added_ids = other_ids - current_ids
        removed_ids = current_ids - other_ids
        common_ids = current_ids & other_ids
        
        for pid in added_ids:
            added.append(other_profiles[pid])
            
        for pid in removed_ids:
            removed.append({'ID': pid, 'PrimaryName': self.get_profile_primary_name(self.profiles[pid])})
            
        for pid in common_ids:
            p_old = self.profiles[pid]
            p_new = other_profiles[pid]
            
            # Simple deep comparison by JSON dump for speed + accuracy
            # Note: We might want a more granular comparison if order of list matters, 
            # but usually XML parsing preserves it or it's irrelevant.
            if json.dumps(p_old, sort_keys=True) != json.dumps(p_new, sort_keys=True):
                modified.append({
                    'ID': pid,
                    'PrimaryName': self.get_profile_primary_name(p_new),
                    'Before': p_old,
                    'After': p_new
                })
        
        return {
            'added': added,
            'removed': removed,
            'modified': modified
        }

    def get_party_type(self, profile):
        """Resolve PartyType from PartySubTypeID via ref_links."""
        pst = profile.get('PartySubTypeID')
        if isinstance(pst, dict):
            pst_id = pst.get('id', '')
        elif isinstance(pst, list) and pst:
            pst_id = pst[0].get('id', '') if isinstance(pst[0], dict) else ''
        else:
            return ''
        links = self.ref_links.get('PartySubType', {}).get(pst_id, {})
        pt_id = links.get('PartyTypeID', '')
        return self.references.get('PartyType', {}).get(pt_id, '')

    def get_subsidiary_bodies(self, profile):
        """Resolve SubsidiaryBody from SanctionsMeasures via ref_links."""
        bodies = []
        for se in profile.get('SanctionsEntries', []):
            for sm in se.get('SanctionsMeasures', []):
                st = sm.get('SanctionsTypeID', {})
                st_id = st.get('id', '') if isinstance(st, dict) else str(st)
                links = self.ref_links.get('SanctionsProgram', {}).get(st_id, {})
                sb_id = links.get('SubsidiaryBodyID', '')
                if sb_id:
                    sb_name = self.references.get('SubsidiaryBody', {}).get(sb_id, '')
                    if sb_name and sb_name not in bodies:
                        bodies.append(sb_name)
        return bodies

    def get_area_codes(self, profile):
        """Extract AreaCode details (text, description, country) from profile locations via ref_links."""
        texts, descs, countries = [], [], []
        loc_ids = set()
        for f in profile.get('Feature', []):
            for fv in f.get('FeatureVersion', []):
                for vl in fv.get('VersionLocation', []):
                    lid = vl.get('LocationID', {})
                    lid_val = lid.get('id', '') if isinstance(lid, dict) else str(lid)
                    if lid_val:
                        loc_ids.add(lid_val)
        for lid in loc_ids:
            ac_id = self.location_area_codes.get(lid)
            if ac_id:
                ac_links = self.ref_links.get('AreaCode', {}).get(ac_id, {})
                texts.append(ac_links.get('_text', ''))
                descs.append(ac_links.get('Description', ''))
                c_id = ac_links.get('CountryID', '')
                if c_id:
                    countries.append(self.references.get('Country', {}).get(c_id, ''))
        return texts, descs, countries

    def _elem_to_dict(self, elem):
        d = {}
        if elem.text and elem.text.strip():
            d['text'] = elem.text.strip()
        for k, v in elem.attrib.items():
            if k.endswith('ID') and not k == 'ID':
                ref_type = k[:-2]
                if ref_type in self.references and v in self.references[ref_type]:
                    d[k] = {"id": v, "value": self.references[ref_type][v]}
                else:
                    d[k] = v
            else:
                d[k] = v
                
        for child in elem:
            tag = child.tag.split('}')[-1]
            child_dict = self._elem_to_dict(child)
            if tag not in d:
                d[tag] = []
            d[tag].append(child_dict)
        return d

    def search_unique(self, query, program_filter=None):
        q = query.lower()
        results = []
        for text, pid in self.search_index:
            if q in text:
                profile = self.profiles[pid]
                if program_filter:
                    profile_programs = set()
                    for se in profile.get('SanctionsEntries', []):
                        for sm in se.get('SanctionsMeasures', []):
                            if sm.get('Comment'):
                                profile_programs.add(sm['Comment'])
                    if not profile_programs.intersection(program_filter):
                        continue
                results.append(profile)
                if len(results) >= 50:
                    break
        return results

    def format_alias_name(self, alias_dict, identity_dict):
        group_map = {}
        for groups in identity_dict.get('NamePartGroups', []):
            for mg in groups.get('MasterNamePartGroup', []):
                for ng in mg.get('NamePartGroup', []):
                    gid = ng.get('ID')
                    tid = ng.get('NamePartTypeID', {})
                    ty = tid.get('value') if isinstance(tid, dict) else str(tid)
                    group_map[gid] = ty
                    
        order_map = {
            "First Name": 1,
            "Middle Name": 2,
            "Patronymic": 3,
            "Matronymic": 4,
            "Last Name": 5,
            "Entity Name": 10,
            "Nickname": 11,
            "Vessel Name": 12,
            "Aircraft Name": 13
        }
        
        parts_list = []
        for dn in alias_dict.get('DocumentedName', []):
            for pt in dn.get('DocumentedNamePart', []):
                for nv in pt.get('NamePartValue', []):
                    if 'text' in nv:
                        gid = nv.get('NamePartGroupID')
                        ty = group_map.get(gid, "Unknown")
                        weight = order_map.get(ty, 99)
                        parts_list.append((weight, nv['text']))
                        
        parts_list.sort(key=lambda x: x[0])
        return " ".join([x[1] for x in parts_list])

    def get_profile_primary_name(self, profile):
        primary_names = []
        try:
            for identity in profile.get('Identity', []):
                for alias in identity.get('Alias', []):
                    if alias.get('Primary') == 'true':
                        formatted = self.format_alias_name(alias, identity)
                        if formatted:
                            primary_names.append(formatted)
        except Exception:
            pass
        return "; ".join(primary_names) if primary_names else "Unknown Name"

db = OFACDatabase()
if os.path.exists(DATA_FILE):
    db.load(DATA_FILE)

class ChunkWriter:
    def __init__(self, wfile): self.wfile = wfile
    def write(self, s): self.wfile.write(s.encode('utf-8'))

class RequestHandler(BaseHTTPRequestHandler):
    
    def _flatten_element(self, elem, prefix=''):
        d = {}
        for k, v in elem.attrib.items():
            d[prefix + k] = [v]
        if elem.text and elem.text.strip():
            d[prefix + 'Text'] = [elem.text.strip()]
        for c in elem:
            ctag = c.tag.split('}')[-1]
            child_d = self._flatten_element(c, prefix + ctag + '_')
            for ck, cv in child_d.items():
                if ck not in d: d[ck] = []
                d[ck].extend(cv)
                
        if prefix == '':
            for k in d:
                d[k] = "; ".join(d[k])
        return d

    def export_references(self):
        import zipfile
        self.send_response(200)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Disposition', 'attachment; filename="ReferenceValueSets.zip"')
        self.end_headers()
        
        mem_zip = io.BytesIO()
        with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for set_name, values_dict in db.references.items():
                if set_name == 'Location': continue
                csv_io = io.StringIO()
                cw = csv.writer(csv_io)
                cw.writerow(["ID", "Value"])
                for k, v in values_dict.items():
                    cw.writerow([k, v])
                zf.writestr(f"{set_name}.csv", csv_io.getvalue().encode('utf-8'))
        
        self.wfile.write(mem_zip.getvalue())

    def export_dataset(self, tag_name):
        self.send_response(200)
        self.send_header('Content-Type', 'text/csv')
        self.send_header('Content-Disposition', f'attachment; filename="{tag_name}.csv"')
        self.end_headers()
        
        global_headers = set()
        expected_child = tag_name[:-1] if tag_name.endswith('s') else tag_name
        if tag_name == 'SanctionsEntries': expected_child = 'SanctionsEntry'
        if tag_name == 'DistinctParties': expected_child = 'DistinctParty'
        
        try:
            context = ET.iterparse(DATA_FILE, events=('start', 'end'))
            in_target = False
            for event, elem in context:
                tag = elem.tag.split('}')[-1]
                if event == 'start' and tag == tag_name:
                    in_target = True
                if event == 'end' and in_target and tag == expected_child:
                    flat = self._flatten_element(elem)
                    global_headers.update(flat.keys())
                    elem.clear()
                if event == 'end' and tag == tag_name:
                    break
        except Exception:
            pass
            
        global_headers = sorted(list(global_headers))
        if not global_headers:
            global_headers = ["No Data Found"]
            
        csv_writer = csv.writer(ChunkWriter(self.wfile))
        csv_writer.writerow(global_headers)
        
        if global_headers[0] == "No Data Found":
            return
            
        try:
            context = ET.iterparse(DATA_FILE, events=('start', 'end'))
            in_target = False
            for event, elem in context:
                tag = elem.tag.split('}')[-1]
                if event == 'start' and tag == tag_name:
                    in_target = True
                if event == 'end' and in_target and tag == expected_child:
                    flat = self._flatten_element(elem)
                    row_vals = [flat.get(h, '') for h in global_headers]
                    csv_writer.writerow(row_vals)
                    elem.clear()
                if event == 'end' and tag == tag_name:
                    break
        except Exception:
            pass

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        
        if path == "/api/status":
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "db_status": "Ready" if len(db.profiles) > 0 else "Reloading...",
                "profile_count": len(db.profiles),
                "feature_types": db.references.get('FeatureType', {}),
                "sanctions_programs": sorted(list(db.sanctions_programs))
            }).encode('utf-8'))
            
        elif path == "/api/template":
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="batch_template.csv"')
            self.end_headers()
            self.wfile.write(b"SearchTerm\n")
            
        elif path == "/api/export/all":
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="ofac_full_database.csv"')
            self.end_headers()
            
            feature_schema = db.references.get('FeatureType', {})
            f_names = sorted(list(set(feature_schema.values())))
            sanctions_cols = ["SanctionsList", "EntryDate", "LegalBasis", "SanctionsPrograms", "SubsidiaryBody"]
            area_code_cols = ["AreaCode_Text", "AreaCode_Description", "AreaCode_Country"]
            headers = ["OFAC_ID", "PrimaryName", "Type", "PartyType", "PartyComment", "Aliases"] + sanctions_cols + area_code_cols + f_names
            
            cw = csv.writer(ChunkWriter(self.wfile))
            cw.writerow(headers)
            
            for pid, p in db.profiles.items():
                primary_name = db.get_profile_primary_name(p)
                pType = p.get("PartySubTypeID", {}).get("value", "") if isinstance(p.get("PartySubTypeID"), dict) else str(p.get("PartySubTypeID", ""))
                
                aliases = []
                for ident in p.get('Identity', []):
                    for alias in ident.get('Alias', []):
                        if alias.get('Primary') != 'true':
                            formatted = db.format_alias_name(alias, ident)
                            if formatted:
                                aliases.append(formatted)
                
                feature_map = {n: [] for n in f_names}
                for f in p.get('Feature', []):
                    ftype = f.get('FeatureTypeID', {}).get('value', '') if isinstance(f.get('FeatureTypeID'), dict) else str(f.get('FeatureTypeID', ''))
                    for fv in f.get('FeatureVersion', []):
                        detail = ""
                        for loc in fv.get('VersionLocation', []):
                            locid = loc.get('LocationID', {})
                            loc_str = locid.get('value') or locid.get('id') if isinstance(locid, dict) else str(locid)
                            if loc_str: detail += loc_str + " "
                        for vd in fv.get('VersionDetail', []):
                            if 'text' in vd: detail += vd['text'] + " "
                            drObj = vd.get('DetailReferenceID', {})
                            drStr = drObj.get('value') or drObj.get('id') if isinstance(drObj, dict) else str(drObj)
                            if drStr and drStr != '{}': detail += drStr + " "
                        for c in fv.get('Comment', []):
                            if 'text' in c: detail += "[" + c['text'] + "] "
                        for dp in fv.get('DatePeriod', []):
                            start = dp.get('Start', [])
                            if start and 'From' in start[0]:
                                from_date = start[0]['From'][0]
                                y = from_date.get('Year', [{}])[0].get('text', '')
                                m = from_date.get('Month', [{}])[0].get('text', '')
                                d = from_date.get('Day', [{}])[0].get('text', '')
                                date_str = "-".join([x for x in [y, m, d] if x])
                                detail += date_str + " "
                        if detail.strip() and ftype in feature_map:
                            feature_map[ftype].append(detail.strip())
                
                se_lists, se_dates, se_legal, se_programs = [], [], [], []
                for se in p.get('SanctionsEntries', []):
                    if se.get('ListName'): se_lists.append(se['ListName'])
                    if se.get('EntryDate'): se_dates.append(se['EntryDate'])
                    if se.get('LegalBasis'): se_legal.append(se['LegalBasis'])
                    for sm in se.get('SanctionsMeasures', []):
                        if sm.get('Comment'): se_programs.append(sm['Comment'])
                
                pPartyType = db.get_party_type(p)
                se_subsidiary = db.get_subsidiary_bodies(p)
                ac_texts, ac_descs, ac_countries = db.get_area_codes(p)

                pComment = p.get('DistinctPartyComment', '')
                row_data = [
                    p.get("ID", ""), primary_name, pType, pPartyType, pComment, "; ".join(aliases),
                    "; ".join(se_lists), "; ".join(se_dates), "; ".join(se_legal), "; ".join(se_programs),
                    "; ".join(se_subsidiary),
                    "; ".join(ac_texts), "; ".join(ac_descs), "; ".join(ac_countries)
                ]
                for fn in f_names:
                    row_data.append("; ".join(feature_map[fn]))
                cw.writerow(row_data)
            
        elif path.startswith("/api/export/"):
            dataset = path.split("/")[-1]
            if dataset == "ReferenceValueSets":
                self.export_references()
            elif dataset in ["Locations", "IDRegDocuments", "ProfileRelationships", "SanctionsEntries", "DistinctParties"]:
                self.export_dataset(dataset)
            else:
                self.send_response(404)
                self.end_headers()
            
        elif path == "/api/search/unique":
            query_params = urllib.parse.parse_qs(parsed_path.query)
            query = query_params.get('q', [''])[0]
            programs_param = query_params.get('programs', [''])[0]
            program_filter = set(programs_param.split(',')) if programs_param else None
            if query:
                results = db.search_unique(query, program_filter=program_filter)
                # Inject resolved PartyType into each result for the frontend
                for r in results:
                    r['_partyType'] = db.get_party_type(r)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(results).encode('utf-8'))
            else:
                self.send_response(400)
                self.end_headers()

        elif path == "/api/export-delta":
            if not last_comparison_result:
                self.send_response(404)
                self.end_headers()
                return
                
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="ofac_delta_report.csv"')
            self.end_headers()
            
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Status", "OFAC_ID", "PrimaryName"])
            
            for p in last_comparison_result['added']:
                writer.writerow(["Added", p.get('ID', ''), db.get_profile_primary_name(p)])
            for p in last_comparison_result['removed']:
                writer.writerow(["Removed", p.get('ID', ''), p.get('PrimaryName', '')])
            for p in last_comparison_result['modified']:
                writer.writerow(["Modified", p.get('ID', ''), p.get('PrimaryName', '')])
                
            self.wfile.write(output.getvalue().encode('utf-8'))
                
        elif path == "/api/search/dataset":
            query_dict = urllib.parse.parse_qs(parsed_path.query)
            dataset = query_dict.get('type', [''])[0]
            search_str = query_dict.get('q', [''])[0].lower()
            
            if not dataset:
                self.send_response(400)
                self.end_headers()
                return
                
            results = self.search_dataset(dataset, search_str)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(results).encode('utf-8'))
            
        elif path == "/":
            self._serve_file("index.html", "text/html")
        elif path == "/style.css":
            self._serve_file("style.css", "text/css")
        elif path == "/script.js":
            self._serve_file("script.js", "application/javascript")
        else:
            self.send_response(404)
            self.end_headers()

    def search_dataset(self, tag_name, query_str):
        if tag_name == 'ReferenceValueSets':
            rows = []
            for ref_name, ref_dict in db.references.items():
                if ref_name == 'Location': continue
                for k, v in ref_dict.items():
                    if not query_str or query_str in str(k).lower() or query_str in str(v).lower():
                        rows.append({"Reference Dataset": ref_name, "ID": k, "Value": v})
            return {"columns": ["Reference Dataset", "ID", "Value"], "rows": rows[:200]}
            
        expected_child = tag_name[:-1] if tag_name.endswith('s') else tag_name
        if tag_name == 'SanctionsEntries': expected_child = 'SanctionsEntry'
        if tag_name == 'DistinctParties': expected_child = 'DistinctParty'
        
        matches = []
        global_headers = set()
        
        try:
            context = ET.iterparse(DATA_FILE, events=('start', 'end'))
            in_target = False
            for event, elem in context:
                tag = elem.tag.split('}')[-1]
                if event == 'start' and tag == tag_name:
                    in_target = True
                if event == 'end' and in_target and tag == expected_child:
                    flat = self._flatten_element(elem)
                    
                    matched = False
                    if not query_str:
                        matched = True
                    else:
                        for v in flat.values():
                            if query_str in str(v).lower():
                                matched = True
                                break
                                
                    if matched:
                        matches.append(flat)
                        global_headers.update(flat.keys())
                        if len(matches) >= 100:
                            break
                    elem.clear()
                if event == 'end' and tag == tag_name:
                    break
        except Exception:
            pass
            
        global_headers = sorted(list(global_headers))
        return {"columns": global_headers, "rows": matches}

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        
        if path == "/api/search/batch":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            # Check for multipart or parse programs header
            programs_header = self.headers.get('X-Programs', '')
            program_filter = set(programs_header.split(',')) if programs_header else None
            
            # Read CSV
            reader = csv.DictReader(io.StringIO(post_data.decode('utf-8')))
            
            output = io.StringIO()
            writer = csv.writer(output)
            
            feature_schema = db.references.get('FeatureType', {})
            f_names = sorted(list(set(feature_schema.values())))
            
            sanctions_cols = ["SanctionsList", "EntryDate", "LegalBasis", "SanctionsPrograms", "SubsidiaryBody"]
            area_code_cols = ["AreaCode_Text", "AreaCode_Description", "AreaCode_Country"]
            headers = ["SearchTerm", "Matched", "OFAC_ID", "PrimaryName", "Type", "PartyType", "PartyComment", "Aliases"] + sanctions_cols + area_code_cols + f_names
            writer.writerow(headers)
            
            for row in reader:
                term = row.get("SearchTerm", "").strip()
                if not term:
                    continue
                results = db.search_unique(term, program_filter=program_filter)
                if results:
                    for p in results:
                        primary_name = db.get_profile_primary_name(p)
                        
                        pType = p.get("PartySubTypeID", {}).get("value", "") if isinstance(p.get("PartySubTypeID"), dict) else str(p.get("PartySubTypeID", ""))
                        
                        aliases = []
                        for ident in p.get('Identity', []):
                            for alias in ident.get('Alias', []):
                                if alias.get('Primary') != 'true':
                                    formatted = db.format_alias_name(alias, ident)
                                    if formatted:
                                        aliases.append(formatted)
                                        
                        feature_map = {n: [] for n in f_names}
                        
                        for f in p.get('Feature', []):
                            ftype = f.get('FeatureTypeID', {}).get('value', '') if isinstance(f.get('FeatureTypeID'), dict) else str(f.get('FeatureTypeID', ''))
                            
                            for fv in f.get('FeatureVersion', []):
                                detail = ""
                                for loc in fv.get('VersionLocation', []):
                                    locid = loc.get('LocationID', {})
                                    loc_str = locid.get('value') or locid.get('id') if isinstance(locid, dict) else str(locid)
                                    if loc_str:
                                        detail += loc_str + " "
                                for vd in fv.get('VersionDetail', []):
                                    if 'text' in vd:
                                        detail += vd['text'] + " "
                                    drObj = vd.get('DetailReferenceID', {})
                                    drStr = drObj.get('value') or drObj.get('id') if isinstance(drObj, dict) else str(drObj)
                                    if drStr and drStr != '{}':
                                        detail += drStr + " "
                                for c in fv.get('Comment', []):
                                    if 'text' in c:
                                        detail += "[" + c['text'] + "] "
                                for dp in fv.get('DatePeriod', []):
                                    start = dp.get('Start', [])
                                    if start and 'From' in start[0]:
                                        from_date = start[0]['From'][0]
                                        y = from_date.get('Year', [{}])[0].get('text', '')
                                        m = from_date.get('Month', [{}])[0].get('text', '')
                                        d = from_date.get('Day', [{}])[0].get('text', '')
                                        date_str = "-".join([x for x in [y, m, d] if x])
                                        detail += date_str + " "
                                
                                if detail.strip() and ftype in feature_map:
                                    feature_map[ftype].append(detail.strip())

                        # Extract sanctions data
                        se_lists = []
                        se_dates = []
                        se_legal = []
                        se_programs = []
                        for se in p.get('SanctionsEntries', []):
                            if se.get('ListName'): se_lists.append(se['ListName'])
                            if se.get('EntryDate'): se_dates.append(se['EntryDate'])
                            if se.get('LegalBasis'): se_legal.append(se['LegalBasis'])
                            for sm in se.get('SanctionsMeasures', []):
                                prog = sm.get('Comment', '')
                                if prog: se_programs.append(prog)

                        pPartyType = db.get_party_type(p)
                        se_subsidiary = db.get_subsidiary_bodies(p)
                        ac_texts, ac_descs, ac_countries = db.get_area_codes(p)

                        pComment = p.get('DistinctPartyComment', '')
                        row_data = [
                            term, "Yes", p.get("ID", ""), primary_name, pType, pPartyType, pComment, "; ".join(aliases),
                            "; ".join(se_lists), "; ".join(se_dates), "; ".join(se_legal), "; ".join(se_programs),
                            "; ".join(se_subsidiary),
                            "; ".join(ac_texts), "; ".join(ac_descs), "; ".join(ac_countries)
                        ]
                        for fn in f_names:
                            row_data.append("; ".join(feature_map[fn]))

                        writer.writerow(row_data)
                else:
                    null_row = [term, "No", "", "", "", "", "", ""] + [""] * len(sanctions_cols) + [""] * len(area_code_cols) + [""] * len(f_names)
                    writer.writerow(null_row)
                    
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="batch_results.csv"')
            self.end_headers()
            self.wfile.write(output.getvalue().encode('utf-8'))

            self.wfile.write(json.dumps({"success": True, "message": "XML Uploaded and database restarted!"}).encode('utf-8'))

        elif path == "/api/compare":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            temp_file = "temp_compare.xml"
            with open(temp_file, "wb") as f:
                f.write(post_data)
                
            global last_comparison_result
            last_comparison_result = db.compare(temp_file)
            
            # Inject _partyType into all profiles for frontend display
            for p in last_comparison_result.get('added', []):
                p['_partyType'] = db.get_party_type(p)
            for m in last_comparison_result.get('modified', []):
                m['Before']['_partyType'] = db.get_party_type(m['Before'])
                m['After']['_partyType'] = db.get_party_type(m['After'])
            
            # Clean up temp file
            try: os.remove(temp_file)
            except: pass
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "summary": {
                    "added": len(last_comparison_result['added']),
                    "removed": len(last_comparison_result['removed']),
                    "modified": len(last_comparison_result['modified'])
                },
                "details": last_comparison_result
            }).encode('utf-8'))

        else:
            self.send_response(404)
            self.end_headers()

    def _serve_file(self, filename, content_type):
        if os.path.exists(filename):
            self.send_response(200)
            self.send_header('Content-type', content_type)
            self.end_headers()
            with open(filename, 'rb') as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), RequestHandler)
    print(f"HTTP Server running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
