/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f7f7f8',
          100: '#ececec',
          200: '#dedede',
          300: '#c9c9c9',
          500: '#6b6b6b',
          600: '#0d0d0d',
          700: '#000000',
          800: '#000000',
        },
      },
    },
  },
  plugins: [],
}
