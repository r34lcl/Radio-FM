#!/bin/sh
# Genera la configuración de Icecast desde variables de entorno y lo arranca.
set -eu

cat > /etc/icecast2/icecast.xml <<XML
<icecast>
    <location>${ICECAST_LOCATION:-Chile}</location>
    <admin>${ICECAST_ADMIN_EMAIL:-admin@localhost}</admin>
    <hostname>${ICECAST_HOSTNAME:-localhost}</hostname>
    <limits>
        <clients>${ICECAST_MAX_CLIENTS:-20}</clients>
        <sources>8</sources>
        <queue-size>524288</queue-size>
        <client-timeout>30</client-timeout>
        <header-timeout>15</header-timeout>
        <source-timeout>10</source-timeout>
        <burst-on-connect>1</burst-on-connect>
        <burst-size>65535</burst-size>
    </limits>
    <authentication>
        <source-password>${ICECAST_SOURCE_PASSWORD}</source-password>
        <relay-password>${ICECAST_SOURCE_PASSWORD}</relay-password>
        <admin-user>admin</admin-user>
        <admin-password>${ICECAST_ADMIN_PASSWORD}</admin-password>
    </authentication>
    <listen-socket>
        <port>8000</port>
    </listen-socket>
    <http-headers>
        <header name="Access-Control-Allow-Origin" value="*" />
    </http-headers>
    <fileserve>1</fileserve>
    <paths>
        <basedir>/usr/share/icecast2</basedir>
        <logdir>/var/log/icecast2</logdir>
        <webroot>/usr/share/icecast2/web</webroot>
        <adminroot>/usr/share/icecast2/admin</adminroot>
        <alias source="/" destination="/status.xsl"/>
    </paths>
    <logging>
        <accesslog>access.log</accesslog>
        <errorlog>-</errorlog>
        <loglevel>3</loglevel>
        <logsize>10000</logsize>
    </logging>
    <security>
        <chroot>0</chroot>
        <changeowner>
            <user>icecast2</user>
            <group>icecast</group>
        </changeowner>
    </security>
</icecast>
XML

chown -R icecast2:icecast /var/log/icecast2
exec icecast2 -c /etc/icecast2/icecast.xml
