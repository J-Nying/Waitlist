import { createApp } from 'vue'
import App from './App.vue'
import Keycloak from 'keycloak-js'

const keycloak = new Keycloak({
  url: 'http://localhost:8080',
  realm: 'Waitlist',
  clientId: 'waitlist',
  onLoad: 'login-required',
  // redirectUri: window.location.origin + '/'
})

// Debug function to catch all Keycloak errors
function initKeycloak() {
  keycloak.init({ checkLoginIframe: false })
    .then(authenticated => {
      console.log('Keycloak init result:', authenticated)

      if (!authenticated) {
        console.warn('Not authenticated — showing login page')
        keycloak.login()
        return
      }

      localStorage.setItem('vue-token', keycloak.token)
      localStorage.setItem('vue-refresh-token', keycloak.refreshToken)

      setInterval(() => {
        keycloak.updateToken(70).catch(() => console.warn('Failed to refresh token'))
      }, 60000)

      // Clean up URL hash
      if (window.location.hash.includes('iss=')) {
        window.history.replaceState(null, '', window.location.pathname)
      }

      // Mount Vue app only after Keycloak init succeeds
      createApp(App).mount('#app')
    })
    .catch(err => {
      console.error('Keycloak init failed:', err)
      alert('Keycloak init failed — check console for details')
    })
}

initKeycloak()
