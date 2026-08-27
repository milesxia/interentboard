FROM nginx:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY app.js /usr/share/nginx/html/app.js
COPY styles.css /usr/share/nginx/html/styles.css

# BEGIN INTERNETBOARD V4.7 FRONTEND BUILD FILES
COPY build.js /usr/share/nginx/html/build.js
COPY build.json /usr/share/nginx/html/build.json
# END INTERNETBOARD V4.7 FRONTEND BUILD FILES
