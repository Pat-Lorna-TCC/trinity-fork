/**
 * Client Portal store (enterprise `client_portal`, epic #78 / #79).
 *
 * Domain store for the client-facing portal surface. First slice: the
 * "My Agents" roster (agents shared with the signed-in email) + the operator
 * exposure config. Backed by the gated `/api/enterprise/client-portal/*`
 * endpoints — 404 in OSS/unentitled builds, but the route guard
 * (`requiresEntitlement: 'client_portal'`) keeps this store off those builds.
 */
import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

const PORTAL_TOKEN_KEY = 'trinity.portalToken'

export const useClientPortalStore = defineStore('clientPortal', {
  state: () => ({
    clientEmail: null,
    agents: [],
    loading: false,
    error: null,
    // A portal session token (verified email, no platform account). When set,
    // it authenticates the portal endpoints; else we fall back to the platform
    // login (operator preview). Persisted so a client stays signed in.
    portalToken: localStorage.getItem(PORTAL_TOKEN_KEY) || null,
  }),

  getters: {
    // Portal-session token wins; otherwise the operator's platform auth header.
    authHeader() {
      if (this.portalToken) return { Authorization: `Bearer ${this.portalToken}` }
      const authStore = useAuthStore()
      return authStore.authHeader
    },
    isClientSignedIn: (state) => !!state.portalToken,
  },

  actions: {
    // Step 1 — request a 6-digit code. Always resolves (generic response); the
    // backend reveals nothing about whether the email has access.
    async requestCode(email) {
      await axios.post('/api/enterprise/client-portal/auth/request', { email })
    },

    // Step 2 — verify the code → portal session token (persisted).
    async verifyCode(email, code) {
      const { data } = await axios.post('/api/enterprise/client-portal/auth/verify', { email, code })
      this.portalToken = data.token
      this.clientEmail = data.email
      localStorage.setItem(PORTAL_TOKEN_KEY, data.token)
      return data
    },

    signOut() {
      this.portalToken = null
      this.clientEmail = null
      this.agents = []
      localStorage.removeItem(PORTAL_TOKEN_KEY)
    },

    // Chat one turn with a rostered agent over the portal session (the gated,
    // roster-scoped endpoint — the OSS chat endpoint fences the portal token).
    // Returns the same `{response, cost}` shape as agentsStore.sendChatMessage
    // so PortalChat.vue can consume either.
    async sendPortalChat(agentName, message) {
      const { data } = await axios.post(
        `/api/enterprise/client-portal/agents/${agentName}/chat`,
        { message },
        { headers: this.authHeader }
      )
      return data
    },

    // Files a rostered agent has shared (FILES-001), each with a download URL
    // (`?sig=` token is the credential — the download route is public).
    async fetchDocuments(agentName) {
      const { data } = await axios.get(
        `/api/enterprise/client-portal/agents/${agentName}/documents`,
        { headers: this.authHeader }
      )
      return data.documents || []
    },

    // Send a file TO a rostered agent (lands in its inbox). Multipart; let the
    // browser set the boundary — only add the portal auth header.
    async uploadDocument(agentName, file) {
      const form = new FormData()
      form.append('file', file)
      const { data } = await axios.post(
        `/api/enterprise/client-portal/agents/${agentName}/documents`,
        form,
        { headers: this.authHeader }
      )
      return data
    },

    async fetchRoster() {
      this.loading = true
      this.error = null
      try {
        const { data } = await axios.get('/api/enterprise/client-portal/my-agents', {
          headers: this.authHeader,
        })
        this.clientEmail = data.client_email || null
        this.agents = data.agents || []
      } catch (err) {
        // An expired/invalid portal token → drop it so the sign-in form returns.
        if (err.response?.status === 401 && this.portalToken) this.signOut()
        this.error = err.response?.data?.detail || 'Failed to load your agents.'
        this.agents = []
      } finally {
        this.loading = false
      }
    },
  },
})
