/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["voyager.py", "www/*.py"],
  theme: {
    extend: {},
  },
  plugins: [
    require('@tailwindcss/forms'),
  ],
}
