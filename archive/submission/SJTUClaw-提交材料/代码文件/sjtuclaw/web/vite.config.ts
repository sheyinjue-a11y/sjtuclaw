import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        workbench: `${root}/index.html`,
        pet: `${root}/pet.html`,
        petChat: `${root}/pet-chat.html`,
      },
    },
  },
});
