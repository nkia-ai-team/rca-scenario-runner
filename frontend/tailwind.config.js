/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Geist",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
        mono: ["Geist Mono", "ui-monospace", "monospace"],
        serif: ["Instrument Serif", "ui-serif", "Georgia", "serif"],
      },
    },
  },
  plugins: [],
};
