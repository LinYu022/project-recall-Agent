import { defineConfig } from "vite";
export default defineConfig({
  server: {
    proxy: Object.fromEntries(
      ["/product", "/tasks", "/memory"].map((p) => [
        p,
        "http://127.0.0.1:8017",
      ]),
    ),
  },
});
