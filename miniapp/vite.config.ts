import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

export default defineConfig({
  base: "/app/",
  plugins: [preact()],
  build: {
    assetsDir: "assets",
    outDir: "dist"
  },
  test: {
    environment: "jsdom"
  }
});
