# Zendesk Talk MCP Setup for Nassim

This project adds read-only Zendesk Talk analytics for Claude Cowork. You do not need credentials for development or automated tests; only add real credentials when you deploy your own copy.

## 1. Create Zendesk credentials

1. Sign in to Zendesk as an admin.
2. Open **Admin Center**.
3. Click **Apps and integrations**.
4. Click **Zendesk API**.
5. Turn on **Token access** if it is not already on.
6. Click **Add API token**.
7. Name it something like `Claude Cowork Talk Read Only`.
8. Copy the token immediately. Zendesk only shows it once.
9. Store it in your password manager until you add it to Render.

## 2. Confirm the account is read-only for this integration

The code only calls `GET` endpoints for Talk analytics and ticket lookups. It does not create, update, or delete Zendesk Talk records.

For the safest setup, create or choose a Zendesk user that can view Talk reporting/tickets but does not have admin or write responsibilities you do not need.

## 3. Environment variables to add

Add these variables in Render, not in code:

| Variable | What to enter |
| --- | --- |
| `ZENDESK_EMAIL` | The Zendesk user's email address |
| `ZENDESK_TOKEN` | The API token copied from Zendesk |
| `ZENDESK_SUBDOMAIN` | Only the subdomain, for example `mycompany` from `mycompany.zendesk.com` |
| `MCP_TRANSPORT` | `http` |
| `MCP_AUTH_MODE` | `oauth` for Claude Cowork |
| `MCP_PUBLIC_BASE_URL` | Your Render URL, for example `https://YOUR-RENDER-SERVICE.onrender.com` |
| `MCP_OAUTH_ISSUER` | Your OAuth provider issuer URL |
| `MCP_OAUTH_AUDIENCE` | Your MCP resource URL, usually `https://YOUR-RENDER-SERVICE.onrender.com/mcp` |
| `MCP_OAUTH_JWKS_URL` | Your OAuth provider JWKS URL |
| `MCP_OAUTH_AUTHORIZATION_ENDPOINT` | Your OAuth provider authorization endpoint |
| `MCP_OAUTH_TOKEN_ENDPOINT` | Your OAuth provider token endpoint |
| `MCP_OAUTH_REQUIRED_SCOPE` | `zendesk:read` |
| `MCP_ALLOWED_EMAIL` | The authorized SameDayCustom user email |
| `MCP_AUTH_TOKEN` | Optional development-only token for Claude Code if `MCP_AUTH_MODE=static`; do not use this as the recommended Claude Cowork auth method |
| `REMOTE_STORAGE_DIR` | Optional managed server-side folder for temporary response files; leave blank to use the safe system temp folder |
| `REMOTE_STORAGE_RETENTION_SECONDS` | Optional cleanup window for remote response files; default is 604800 seconds / 7 days |

Never paste real credentials into GitHub, chat, screenshots, logs, or support tickets.

## 3A. Create the Claude Cowork OAuth app

Use your company OAuth provider (for example Auth0, Okta, WorkOS, or another provider that supports OAuth/OIDC).

1. Open the OAuth provider admin dashboard.
2. Create a new application.
3. Choose a regular web/confidential application if the provider asks for an app type.
4. Enable **Authorization Code**.
5. Enable **PKCE** with method `S256`.
6. Add the redirect/callback URL shown by Claude Cowork when you create the connector.
7. Add or allow the scope `zendesk:read`.
8. Copy the Client ID and Client Secret for Claude Cowork.
9. Copy the issuer URL, authorization endpoint, token endpoint, and JWKS URL for Render.
10. Configure the app so only the SameDayCustom user/account you approve can complete login.

## 4. Deploy through Render

1. Push this branch/repository to GitHub.
2. Log in to Render.
3. Click **New +**.
4. Click **Web Service**.
5. Connect the GitHub repository.
6. Choose the branch with this feature.
7. Render should detect the included `render.yaml`/Dockerfile setup.
8. Add all environment variables listed above.
9. Click **Create Web Service**.
10. Wait for the deploy to finish.
11. Open `https://YOUR-RENDER-SERVICE.onrender.com/health` in a browser.
12. You should see a safe response like `{"status":"ok"}`. It contains no Zendesk data or secrets.

## 5. Connect to Claude Cowork

1. Open Claude Cowork settings.
2. Go to MCP / connectors / integrations.
3. Add a remote MCP server.
4. Use this URL: `https://YOUR-RENDER-SERVICE.onrender.com/mcp`.
5. Choose OAuth authentication in Claude Cowork/custom connector setup.
6. Enter the pre-registered OAuth Client ID and Client Secret from your OAuth provider.
7. Use authorization code with PKCE. The MCP server publishes discovery metadata at `/.well-known/oauth-protected-resource/mcp` and points Claude to your OAuth provider.
8. Save the connector.
9. Ask Claude to list Zendesk MCP tools and confirm Talk tools are available.

## 6. Test the Talk connection

Ask Claude Cowork:

> Use Zendesk Talk analytics for 2026-01-01 through 2026-01-02 and summarize call outcomes by date.

Expected behavior:

- Claude should call the Talk analytics MCP tool.
- The integration should only read Zendesk data.
- Results should include a saved local response path in the service container and grouped counts.


## 6A. Understand Talk date windows

There are two different dates in Zendesk Talk reports:

- **Data retrieval/update window**: the start and end dates tell Zendesk which Talk records were created or updated during that export window. This catches older calls that were completed or updated later.
- **Call occurrence/reporting window**: date and hour breakdowns use when the customer actually called, such as `started_at` or `call_started_at`, not the later `updated_at` value.

This means an older call updated today can be retrieved for audit completeness, but it will still be grouped under the day/hour when the call actually happened. It will not be counted as a brand-new call today just because Zendesk updated the record today.

## 7. Revoke access

1. In Zendesk Admin Center, open **Apps and integrations → Zendesk API**.
2. Find the API token used for Claude Cowork.
3. Delete/revoke that token.
4. In Render, delete or replace `ZENDESK_TOKEN`.
5. Redeploy or restart the Render service.

## 8. Confirm it remains read-only

- The Claude Cowork remote `/mcp` endpoint uses a dedicated read-only MCP tool registry. It does not register ticket creation, ticket update, private/public comment, deletion, or other Zendesk mutation tools.
- Remote users cannot choose file destinations. Remote tools do not expose `output_path`, `file_path`, or directory path inputs; any temporary response files are generated by the server inside `REMOTE_STORAGE_DIR` or a safe system temp folder. The managed directory is forced to owner-only `0700` permissions, stored response files are forced to owner-only `0600` permissions, and expired managed JSON response files are cleaned up after `REMOTE_STORAGE_RETENTION_SECONDS` (7 days by default).
- The Talk feature calls only:
  - `GET /api/v2/channels/voice/stats/incremental/calls`
  - `GET /api/v2/channels/voice/stats/incremental/legs`
- Pagination is limited to Zendesk `next_page` URLs on your configured subdomain.
- The health endpoint does not expose account data.
- Automated tests use fake Zendesk responses and do not require real credentials.

## 9. Troubleshooting

### 401 Authentication failed

Check `ZENDESK_EMAIL`, `ZENDESK_TOKEN`, and `ZENDESK_SUBDOMAIN`. Make sure the token was copied correctly and belongs to that email.

### 403 Permission denied

The Zendesk user probably cannot access Talk analytics. Give that user the minimum reporting/ticket access needed, or use another read-capable user.

### 429 Rate limited

Zendesk asked the service to slow down. The code waits between Talk requests and honors Zendesk's `Retry-After` header. Try a smaller date range if this repeats.

### 5xx Zendesk server error

Zendesk may be temporarily unavailable. Wait and retry later.

### Claude cannot connect

Check that the URL ends in `/mcp`, the service is deployed, and `MCP_AUTH_MODE=oauth` is set for Claude Cowork. Confirm your OAuth Client ID and Client Secret are the pre-registered values from your OAuth provider.

### OAuth login fails

Confirm the OAuth provider allows authorization code with PKCE, the redirect URI required by Claude Cowork is registered in the OAuth provider, and the requested scope includes `zendesk:read`.

### Health check works but MCP does not

Confirm `MCP_OAUTH_ISSUER`, `MCP_OAUTH_AUDIENCE`, `MCP_OAUTH_JWKS_URL`, `MCP_OAUTH_AUTHORIZATION_ENDPOINT`, `MCP_OAUTH_TOKEN_ENDPOINT`, and `MCP_ALLOWED_EMAIL` are set correctly. The MCP server returns `401` with a `WWW-Authenticate` header pointing to protected-resource metadata when OAuth is required.
