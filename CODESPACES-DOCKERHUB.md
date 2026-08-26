# Codespaces -> GitHub Actions -> Docker Hub

A push to `main` triggers `.github/workflows/dockerhub.yml`. CI validates the production invariants and then publishes:

- `milesxia/internetboard-backend:latest`
- `milesxia/internetboard-worker:latest`
- `milesxia/internetboard-scheduler:latest`
- `milesxia/internetboard-frontend:latest`

The backend build uses the repository root as its Docker build context so `config/` and `seed/` are packaged into the image. QNAP only pulls published images.

Docker Hub credentials remain GitHub Actions repository secrets and are not stored in source code.
