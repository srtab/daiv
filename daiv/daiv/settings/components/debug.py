import socket

# To allow docker container being considered as in debug

hostname, _, ips = socket.gethostbyname_ex(socket.gethostname())
INTERNAL_IPS = [ip[:-1] + "1" for ip in ips]

# Local development only (this component is not included by production/test
# settings). Production fails closed: common.py defaults DJANGO_ALLOWED_HOSTS to
# the empty list, so the app refuses to serve until the operator configures hosts.
ALLOWED_HOSTS = ["*"]
