# This folder has its own Dockerfile, so deploy.py leaves it alone and builds
# this instead. Swap nginx for Node, Python, Go — anything that listens on a
# port. Set that port in site.json with "port": 3000 and Caddy follows.
FROM nginx:1.27-alpine

COPY index.html index.css script.js /usr/share/nginx/html/

# Serve a tiny health endpoint separate from the page itself.
RUN printf 'server {\n\
  listen 80;\n\
  root /usr/share/nginx/html;\n\
  location = /healthz { return 200 "ok\\n"; add_header Content-Type text/plain; }\n\
  location / { try_files $uri $uri/ /index.html; }\n\
}\n' > /etc/nginx/conf.d/default.conf

EXPOSE 80
