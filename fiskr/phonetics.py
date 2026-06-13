import re

def is_vowel(char: str) -> bool:
    return char in "AEIOUY"

def double_metaphone(name: str) -> tuple[str, str]:
    """
    Computes the primary and secondary Double Metaphone keys for a given name.
    This is a pure-Python implementation based on Lawrence Philips' algorithm.
    """
    if not name:
        return ("", "")

    # Clean the input: uppercase, keep only letters, spaces to separate
    name = name.upper()
    name = re.sub(r"[^A-Z\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    
    if not name:
        return ("", "")
        
    # Double Metaphone processes the first word for "PHONETIC_FIRST"
    words = name.split()
    word = words[0] if words else ""
    if not word:
        return ("", "")
        
    length = len(word)
    current = 0
    primary = []
    secondary = []
    
    # Pad word to avoid out-of-bounds checks
    padded = word + "     "
    
    # Helper functions for context
    def at(idx: int) -> str:
        if 0 <= idx < length:
            return padded[idx]
        return ""
        
    def substr(idx: int, length_sub: int) -> str:
        if 0 <= idx < length:
            return padded[idx:idx + length_sub]
        return ""

    # Skip initial silent letters
    if substr(0, 2) in ["GN", "KN", "PN", "WR", "PS"]:
        current = 1
    elif at(0) == "X":
        primary.append("S")
        secondary.append("S")
        current = 1
        
    while current < length:
        char = at(current)
        
        # Vowels
        if is_vowel(char):
            if current == 0:
                primary.append("A")
                secondary.append("A")
            current += 1
            continue
            
        # Consonants
        if char == "B":
            primary.append("P")
            secondary.append("P")
            if at(current + 1) == "B":
                current += 2
            else:
                current += 1
                
        elif char == "C":
            # Various C sounds: CH, CI, CE, CY, etc.
            if current > 1 and not is_vowel(at(current - 2)) and substr(current - 1, 3) == "ACH" and \
               at(current + 2) != "I" and (at(current + 2) != "E" or substr(current - 2, 6) in ["BACHER", "MACHER"]):
                primary.append("K")
                secondary.append("K")
                current += 2
            elif current == 0 and substr(0, 6) == "CAESAR":
                primary.append("S")
                secondary.append("S")
                current += 2
            elif substr(current, 2) == "CH":
                if current > 0 and substr(current, 4) == "CHAE":
                    primary.append("K")
                    secondary.append("X")
                    current += 2
                elif current == 0 and (substr(current + 1, 5) in ["HARAC", "HARIS"] or substr(current + 1, 3) in ["HOR", "HYM", "HIA", "HEM"]) and substr(0, 5) != "CHORE":
                    primary.append("K")
                    secondary.append("K")
                    current += 2
                else:
                    primary.append("X")
                    secondary.append("X")
                    current += 2
            elif substr(current, 2) == "CZ" and substr(current - 2, 4) != "WICZ":
                primary.append("S")
                secondary.append("X")
                current += 2
            elif substr(current, 3) == "CIA":
                primary.append("X")
                secondary.append("X")
                current += 3
            elif substr(current, 2) == "CC" and not (current == 1 and at(0) == "M"):
                if at(current + 2) in ["I", "E", "H"] and substr(current + 2, 2) != "HU":
                    if (current == 1 and at(current - 1) == "A") or substr(current - 1, 5) in ["UCCEE", "UCCES"]:
                        primary.append("KS")
                        secondary.append("KS")
                    else:
                        primary.append("X")
                        secondary.append("X")
                    current += 3
                else:
                    primary.append("K")
                    secondary.append("K")
                    current += 2
            elif substr(current, 2) in ["CK", "CG", "CX"]:
                primary.append("K")
                secondary.append("K")
                current += 2
            elif substr(current, 2) in ["CI", "CE", "CY"]:
                primary.append("S")
                secondary.append("S")
                current += 2
            else:
                primary.append("K")
                secondary.append("K")
                if at(current + 1) in [" C", "Q", "G"]:
                    current += 3
                elif at(current + 1) == "C":
                    current += 2
                else:
                    current += 1
                    
        elif char == "D":
            if substr(current, 2) == "DG":
                if at(current + 2) in ["I", "E", "Y"]:
                    primary.append("J")
                    secondary.append("J")
                    current += 3
                else:
                    primary.append("TK")
                    secondary.append("TK")
                    current += 2
            elif substr(current, 2) in ["DT", "DD"]:
                primary.append("T")
                secondary.append("T")
                current += 2
            else:
                primary.append("T")
                secondary.append("T")
                current += 1
                
        elif char == "F":
            primary.append("F")
            secondary.append("F")
            if at(current + 1) == "F":
                current += 2
            else:
                current += 1
                
        elif char == "G":
            if at(current + 1) == "H":
                if current > 0 and not is_vowel(at(current - 1)):
                    primary.append("K")
                    secondary.append("K")
                    current += 2
                elif current == 0:
                    if at(current + 2) == "I":
                        primary.append("J")
                        secondary.append("J")
                    else:
                        primary.append("K")
                        secondary.append("K")
                    current += 2
                elif (current > 1 and at(current - 2) in ["B", "H", "D"]) or \
                     (current > 2 and at(current - 3) in ["B", "H", "D"]) or \
                     (current > 3 and at(current - 4) in ["B", "H"]):
                    current += 2
                else:
                    if current > 2 and at(current - 1) == "U" and at(current - 3) in ["C", "G", "L", "R", "T"]:
                        primary.append("F")
                        secondary.append("F")
                    elif current > 0 and at(current - 1) != "I":
                        primary.append("K")
                        secondary.append("K")
                    current += 2
            elif at(current + 1) == "N":
                if current == 1 and is_vowel(at(0)) and not is_vowel(at(current + 2)):
                    primary.append("KN")
                    secondary.append("N")
                elif substr(current + 2, 2) != "EY" and at(current + 1) != "Y" and not is_vowel(at(current + 2)):
                    primary.append("N")
                    secondary.append("KN")
                else:
                    primary.append("KN")
                    secondary.append("KN")
                current += 2
            elif substr(current, 2) == "GL" and is_vowel(at(current + 2)):
                primary.append("KL")
                secondary.append("L")
                current += 2
            elif substr(current, 2) in ["GE", "GI", "GY"]:
                primary.append("K")
                secondary.append("J")
                current += 2
            elif at(current + 1) == "G":
                primary.append("K")
                secondary.append("K")
                current += 2
            else:
                primary.append("K")
                secondary.append("K")
                current += 1
                
        elif char == "H":
            # Only keep if at start or followed by vowel and not preceded by vowel
            if (current == 0 or is_vowel(at(current - 1))) and is_vowel(at(current + 1)):
                primary.append("H")
                secondary.append("H")
                current += 2
            else:
                current += 1
                
        elif char == "J":
            if substr(current, 4) == "JOSE" or substr(current, 4) == "SAN ":
                primary.append("H")
                secondary.append("H")
                current += 1
            elif current == 0:
                primary.append("J")
                secondary.append("A")
                current += 1
            elif is_vowel(at(current - 1)) and not is_vowel(at(current + 1)):
                primary.append("J")
                secondary.append("H")
                current += 1
            else:
                primary.append("J")
                secondary.append("J")
                if at(current + 1) == "J":
                    current += 2
                else:
                    current += 1
                    
        elif char == "K":
            primary.append("K")
            secondary.append("K")
            if at(current + 1) == "K":
                current += 2
            else:
                current += 1
                
        elif char == "L":
            primary.append("L")
            secondary.append("L")
            if at(current + 1) == "L":
                # Special cases where LL is silent/different in French/Spanish
                current += 2
            else:
                current += 1
                
        elif char == "M":
            primary.append("M")
            secondary.append("M")
            if at(current + 1) == "M" or (at(current + 1) == "B" and (current + 1 == length - 1 or substr(current + 2, 2) == "ER")):
                current += 2
            else:
                current += 1
                
        elif char == "N":
            primary.append("N")
            secondary.append("N")
            if at(current + 1) == "N":
                current += 2
            else:
                current += 1
                
        elif char == "P":
            if at(current + 1) == "H":
                primary.append("F")
                secondary.append("F")
                current += 2
            else:
                primary.append("P")
                secondary.append("P")
                if at(current + 1) == "P":
                    current += 2
                else:
                    current += 1
                    
        elif char == "Q":
            primary.append("K")
            secondary.append("K")
            if at(current + 1) == "Q":
                current += 2
            else:
                current += 1
                
        elif char == "R":
            primary.append("R")
            secondary.append("R")
            if at(current + 1) == "R":
                current += 2
            else:
                current += 1
                
        elif char == "S":
            if substr(current, 2) in ["SH", "SI", "SZ"]:
                primary.append("X")
                secondary.append("X")
                current += 2
            elif substr(current, 3) == "SCH":
                if substr(current + 3, 2) in ["ER", "EN"]:
                    primary.append("X")
                    secondary.append("X")
                else:
                    primary.append("X")
                    secondary.append("K")
                current += 3
            elif current == 0 and substr(0, 5) == "SUGAR":
                primary.append("X")
                secondary.append("S")
                current += 1
            else:
                primary.append("S")
                secondary.append("S")
                if at(current + 1) in ["S", "Z"]:
                    current += 2
                else:
                    current += 1
                    
        elif char == "T":
            if substr(current, 2) == "TION":
                primary.append("X")
                secondary.append("X")
                current += 3
            elif substr(current, 2) in ["TH", "TCH"]:
                # German/English distinction
                primary.append("0")
                secondary.append("T")
                current += 2
            else:
                primary.append("T")
                secondary.append("T")
                if at(current + 1) in ["T", "D"]:
                    current += 2
                else:
                    current += 1
                    
        elif char == "V":
            primary.append("F")
            secondary.append("F")
            if at(current + 1) == "V":
                current += 2
            else:
                current += 1
                
        elif char == "W":
            if substr(current, 2) == "WR":
                primary.append("R")
                secondary.append("R")
                current += 2
            elif current == 0 and (is_vowel(at(1)) or substr(0, 2) == "WH"):
                primary.append("A")
                secondary.append("F")
                current += 1
            else:
                # silent
                current += 1
                
        elif char == "X":
            if not (current == length - 1 and (at(current - 1) in ["I", "E"] or substr(current - 2, 2) in ["AU", "OU"])):
                primary.append("KS")
                secondary.append("KS")
            current += 1
            
        elif char == "Z":
            primary.append("S")
            secondary.append("S")
            if at(current + 1) == "Z":
                current += 2
            else:
                current += 1
        else:
            current += 1
            
    p_key = "".join(primary)[:4]
    s_key = "".join(secondary)[:4]
    return p_key, s_key
