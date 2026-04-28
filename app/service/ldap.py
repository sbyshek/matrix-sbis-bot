from ldap3 import Server, Connection, ALL, SUBTREE
import config
import re
from logger_config import setup_logging, get_logger


setup_logging(config)
logger = get_logger(__name__)

# import logging
# logger = logging.getLogger(__name__)



search_filter = '(&(objectClass=computer)(employeeNumber=a.ivanov))'
server_address = config.LDAP_SERVER
domain_user = config.LDAP_USER
password = config.LDAP_PASSWORD
search_base = config.LDAP_SEARCHBASE
attributes_to_fetch = ['cn', 'description', 'employeeNumber','lastLogonTimestamp','msDS-PrimaryComputer','memberOf']

class LdapSearcher:
    def __init__(self, server_address=server_address, domain_user=domain_user, password=password):
        self.server = Server(server_address, get_info=ALL)
        self.conn = Connection(self.server, user=domain_user, password=password, auto_bind=True)

    def search(self, search_base=search_base, search_filter=search_filter, attributes_to_fetch=attributes_to_fetch):
        self.conn.search(search_base=search_base,
                         search_filter=search_filter,
                         search_scope=SUBTREE,
                         attributes=attributes_to_fetch)
        return self.conn.entries
    
    @staticmethod
    def extract_cn_from_dn(dn_string: str) -> str:
        """Extract computer name from DN string.
        
        Example: CN=OF-IT777,OU=Отдел ИТ(офис),... -> OF-IT777
        """
        if not dn_string:
            return None
        try:
            # Simplified parsing: strip whitespace, split by comma, take first part, then split by '=' and take the second
            first = dn_string.strip().split(',', 1)[0]
            parts = first.split('=', 1)
            if len(parts) == 2:
                # strip possible surrounding whitespace
                return parts[1].strip()
        except Exception as e:
            logger.debug(f"Error extracting CN from DN: {e}")
        return None
    
    def search_and_parse(self, search_base=search_base, search_filter=search_filter, attributes_to_fetch=attributes_to_fetch):
        entries = self.search(search_base, search_filter, attributes_to_fetch)

        if not entries:
            logger.debug(f"No LDAP entries found for filter: {search_filter}")
            return None

        # For user searches, get primary computer name from first matching user
        entry = entries[0]

        # Build a raw attributes dict for diagnostics
        raw_attrs = {}
        try:
            for name in getattr(entry, 'entry_attributes', []):
                try:
                    attr_obj = entry[name]
                    val = getattr(attr_obj, 'value', None)
                    if val is None:
                        try:
                            val = entry.get_attribute(name)
                        except Exception:
                            val = None
                    raw_attrs[name] = val
                except Exception:
                    # best-effort
                    try:
                        raw_attrs[name] = entry.get(name)
                    except Exception:
                        raw_attrs[name] = None
        except Exception:
            pass

        raw_info = {
            'dn': getattr(entry, 'entry_dn', None) or getattr(entry, 'distinguishedName', None),
            'attributes': raw_attrs,
        }

        try:

            # Try to get msDS-PrimaryComputer attribute and extract CN
            mds_primary = None

            # 1) Try direct attribute access variations on entry object
            try:
                if hasattr(entry, 'msDS_PrimaryComputer'):
                    mds_primary = entry.msDS_PrimaryComputer
            except Exception:
                pass

            # 2) Try entry.get_attribute()
            if not mds_primary:
                try:
                    mds_primary = entry.get_attribute('msDS-PrimaryComputer')
                except Exception:
                    mds_primary = None

            # 3) Fallback: check raw_attrs we already collected (case-insensitive key match)
            if not mds_primary and raw_attrs:
                for k, v in raw_attrs.items():
                    if k and k.lower().replace('_', '-') == 'msds-primarycomputer':
                        mds_primary = v
                        break

            # If we found the attribute, extract DN and parse CN
            if mds_primary:
                primary_computer_value = mds_primary.value if hasattr(mds_primary, 'value') else mds_primary
                if isinstance(primary_computer_value, list) and primary_computer_value:
                    primary_computer_value = primary_computer_value[0]
                if primary_computer_value:
                    computer_name = self.extract_cn_from_dn(str(primary_computer_value))
                    if computer_name:
                        logger.info(f"Найден компьютер из msDS-PrimaryComputer: {computer_name}")
                        return computer_name, raw_info

            logger.debug("msDS-PrimaryComputer not found or empty")
            # IMPORTANT: do NOT fallback to `cn` for user searches.
            # If msDS-PrimaryComputer is missing, treat as absent so callers
            # can notify admins and avoid using invalid names (e.g. full user CN).

        except Exception as e:
            logger.error(f"Error parsing LDAP entry: {e}", exc_info=True)

        return None, raw_info

    def is_user_in_group(self, user_login: str, group_identifier: str):
        """Check if user (sAMAccountName) is member of AD group.

        group_identifier may be CN (e.g. 'SBIS_BOT_USERS') or full DN fragment.
        Returns (bool, raw_info_or_none).
        """
        try:
            # search for the user and reuse search_and_parse to collect raw attrs
            search_filter = f'(&(objectClass=person)(sAMAccountName={user_login}))'
            comp, raw = self.search_and_parse(search_filter=search_filter)
            if not raw:
                return False, None

            attrs = raw.get('attributes', {}) or {}
            # memberOf may be available as list or single string
            member_of = attrs.get('memberOf') or attrs.get('memberof')
            if not member_of:
                return False, raw

            # normalize to list
            if isinstance(member_of, str):
                member_list = [member_of]
            else:
                try:
                    member_list = list(member_of)
                except Exception:
                    member_list = [member_of]

            # check each DN: match if group_identifier equals the CN of the DN or is substring
            gid = (group_identifier or '').strip().lower()
            for dn in member_list:
                if not dn:
                    continue
                dn_str = str(dn)
                # exact CN match
                cn = self.extract_cn_from_dn(dn_str)
                if cn and cn.lower() == gid:
                    return True, raw
                # substring match (allow providing full DN in config)
                if gid and gid in dn_str.lower():
                    return True, raw

            return False, raw
        except Exception as e:
            logger.debug(f"Error checking group membership for {user_login}: {e}")
            return False, None
            
        
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self, 'conn') and self.conn:
            self.conn.unbind()
        
    
