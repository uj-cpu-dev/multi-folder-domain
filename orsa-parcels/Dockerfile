# This folder has its own Dockerfile, so deploy.py uses it untouched and
# does not generate one. It is the stock nginx image plus nginx.conf, which
# is what makes /tracking/ work as a path on the same domain.
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY . /usr/share/nginx/html

# These three are build inputs, not pages. Nobody should be able to fetch them.
RUN rm -f /usr/share/nginx/html/site.json \
          /usr/share/nginx/html/Dockerfile \
          /usr/share/nginx/html/nginx.conf \
 && rm -f /usr/share/nginx/html/.dockerignore \
 && nginx -t

EXPOSE 80