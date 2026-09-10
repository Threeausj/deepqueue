import { defineConfig } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Workflow } from "lucide-react";

export default defineConfig({
  plugins: [
    {
      name: "deepqueue-icon",
      transformIndexHtml: () => [
        {
          tag: "link",
          attrs: {
            rel: "icon",
            href: `data:image/svg+xml,${encodeURIComponent(renderToStaticMarkup(createElement(Workflow, { color: "#177c60" })))}`,
          },
        },
      ],
    },
  ],
  build: { outDir: "../src/deepqueue/web_static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
