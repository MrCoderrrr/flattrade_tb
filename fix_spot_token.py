import re

with open('natgas_strategy_v1.py', 'r') as f:
    code = f.read()

old_search = '''                    if "NATURALGAS" in tsym and "MINI" not in tsym and "FUT" in tsym:
                        exd = item.get("exd")
                        if exd:
                            try:
                                dt = datetime.datetime.strptime(exd, "%d-%b-%Y")
                                candidates.append((dt, item))
                            except ValueError:
                                pass
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    now_date = get_ist_now().date()
                    valid_cands = [c for c in candidates if c[0].date() >= now_date]
                    best_cand = valid_cands[0][1] if valid_cands else candidates[-1][1]
                    self.spot_token = best_cand.get("token")
                    self.spot_tsym = str(best_cand.get("tsym", "")).upper()
                    log_info(f"Resolved Spot Symbol (Nearest Expiry {best_cand.get('exd')}): {self.spot_tsym} (Token: {self.spot_token})") # Bug 5 fix'''

new_search = '''                    # MCX Futures symbols don't always contain "FUT", but they do lack "CE" and "PE"
                    if "NATURALGAS" in tsym and "MINI" not in tsym and "CE" not in tsym and "PE" not in tsym:
                        exd = item.get("exd")
                        if exd:
                            try:
                                dt = datetime.datetime.strptime(exd, "%d-%b-%Y")
                                candidates.append((dt, item))
                            except ValueError:
                                pass
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    now_date = get_ist_now().date()
                    valid_cands = [c for c in candidates if c[0].date() >= now_date]
                    best_cand = valid_cands[0][1] if valid_cands else candidates[-1][1]
                    self.spot_token = best_cand.get("token")
                    self.spot_tsym = str(best_cand.get("tsym", "")).upper()
                    log_info(f"Resolved Spot Symbol (Nearest Expiry {best_cand.get('exd')}): {self.spot_tsym} (Token: {self.spot_token})")
                else:
                    first_few = [str(x.get("tsym")) for x in res[:5]] if isinstance(res, list) else []
                    log_warn(f"Failed to find any matching futures token. Search API returned: {first_few}")'''

code = code.replace(old_search, new_search)

with open('natgas_strategy_v1.py', 'w') as f:
    f.write(code)

