FROM nginx:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY classic.html /usr/share/nginx/html/classic.html
COPY command-center.css /usr/share/nginx/html/command-center.css
COPY command-center.js /usr/share/nginx/html/command-center.js
COPY app.js /usr/share/nginx/html/app.js
COPY styles.css /usr/share/nginx/html/styles.css

# BEGIN INTERNETBOARD V4.7 FRONTEND BUILD FILES
COPY build.js /usr/share/nginx/html/build.js
COPY build.json /usr/share/nginx/html/build.json
# END INTERNETBOARD V4.7 FRONTEND BUILD FILES

# BEGIN INTERNETBOARD V4.8 INTELLIGENCE FILES
COPY insights.html /usr/share/nginx/html/insights.html
COPY insights.js /usr/share/nginx/html/insights.js
COPY insights.css /usr/share/nginx/html/insights.css
COPY task-overlay.js /usr/share/nginx/html/task-overlay.js
# END INTERNETBOARD V4.8 INTELLIGENCE FILES
