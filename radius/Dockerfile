# Builds on the official FreeRADIUS 3.2 image rather than reinventing
# radiusd.conf/proxy.conf/policy.conf from scratch — only the pieces this
# platform actually changes are overridden.
#
# Config lives in /etc/raddb, the path the official image documents for
# overrides. (/etc/freeradius/3.0 is the Debian *distro* package layout, not
# this image's — files copied there are never read.)
#
# MikroTik attributes (Mikrotik-Rate-Limit, Mikrotik-Address-List, ...) come
# from the stock dictionary set, which $INCLUDEs dictionary.mikrotik.
FROM freeradius/freeradius-server:3.2.5

COPY sites-enabled/default /etc/raddb/sites-enabled/default
COPY mods-enabled/rest /etc/raddb/mods-enabled/rest
COPY clients.conf /etc/raddb/clients.conf
COPY entrypoint.sh /usr/local/bin/engine-radius-entrypoint
RUN chmod +x /usr/local/bin/engine-radius-entrypoint \
    && rm -f /etc/raddb/sites-enabled/inner-tunnel \
    && mkdir -p /etc/freeradius-clients

# PPPoE/PAP-only per the A5 decision: no EAP, no inner-tunnel.
EXPOSE 1812/udp 1813/udp
ENTRYPOINT ["/usr/local/bin/engine-radius-entrypoint"]
