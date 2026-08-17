# Builds on the official FreeRADIUS 3.2 image rather than reinventing
# radiusd.conf/proxy.conf/policy.conf from scratch — only the pieces this
# platform actually changes are overridden.
FROM freeradius/freeradius-server:3.2.5

COPY sites-enabled/default /etc/freeradius/3.0/sites-enabled/default
COPY mods-enabled/rest /etc/freeradius/3.0/mods-enabled/rest
COPY clients.conf /etc/freeradius/3.0/clients.conf
COPY dictionary.local /etc/freeradius/3.0/dictionary.local

# PPPoE/PAP-only per the A5 decision: no EAP, no inner-tunnel.
RUN rm -f /etc/freeradius/3.0/sites-enabled/inner-tunnel

EXPOSE 1812/udp 1813/udp 3799/udp
