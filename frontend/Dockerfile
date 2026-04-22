# -----------------------------------------------------------------------------
# Whumpf frontend — Vite dev server
#
# This is a *development* image; it runs `vite dev` with host 0.0.0.0 so the
# container port is reachable from the host. For production, replace this with
# a multi-stage build that does `npm run build` and copies dist/ into nginx.
# -----------------------------------------------------------------------------
FROM node:22-alpine

WORKDIR /app

# Install dependencies first for layer caching.
COPY package.json package-lock.json* ./
RUN npm install

# Copy the rest of the project.
COPY . .

EXPOSE 5173

# --host 0.0.0.0 is required so the port binding is reachable outside the
# container; Vite defaults to 127.0.0.1.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
