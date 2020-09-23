{%- macro continuous_reschedule() %}
    restart {
      attempts = 5
      delay    = "11s"
      interval = "3m"
      mode     = "fail"
    }
    reschedule {
      attempts       = 0
      delay          = "11s"
      delay_function = "exponential"
      max_delay      = "19m"
      unlimited      = true
    }
{%- endmacro %}

{%- macro shutdown_delay() %}
      shutdown_delay = "0s"
      kill_timeout = "29s"
{%- endmacro %}

{%- macro task_logs() %}
logs {
  max_files     = 3
  max_file_size = 3
}
{%- endmacro %}

{%- macro group_disk(size=20) %}
ephemeral_disk {
  size = ${size}
}
{%- endmacro %}

{%- macro authproxy_group(name, host, upstream, threads=24, memory=300, user_header_template="{}", count=1, extra_header = false) %}
  group "authproxy" {
    ${ group_disk() }
    spread { attribute = {% raw %}"${attr.unique.hostname}"{% endraw %} }

    restart {
      interval = "2m"
      attempts = 4
      delay = "20s"
      mode = "delay"
    }

    count = ${count}

    task "authproxy-web" {
      ${ task_logs() }

      affinity {
        attribute = "{% raw %}${meta.liquid_large_databases}{% endraw %}"
        value     = "true"
        weight    = -99
      }

      driver = "docker"
      config {
        image = "${config.image('liquid-authproxy')}"
        volumes = [
          ${liquidinvestigations_authproxy_repo}
        ]
        labels {
          liquid_task = "${name}-authproxy"
        }
        port_map {
          authproxy = 5000
        }
        memory_hard_limit = ${memory * 10}

      }
      template {
        data = <<-EOF
          {{- with secret "liquid/${name}/auth.oauth2" }}
            OAUTH2_PROXY_CLIENT_ID = {{.Data.client_id | toJSON }}
            OAUTH2_PROXY_CLIENT_SECRET = {{.Data.client_secret | toJSON }}
          {{- end }}
          {{- with secret "liquid/${name}/cookie" }}
            OAUTH2_PROXY_COOKIE_SECRET = {{.Data.cookie | toJSON }}
          {{- end }}
            OAUTH2_PROXY_EMAIL_DOMAINS = *
            OAUTH2_PROXY_HTTP_ADDRESS = "0.0.0.0:5000"
            OAUTH2_PROXY_PROVIDER = "liquid"
            OAUTH2_PROXY_COOKIE_HTTPONLY = false
            OAUTH2_PROXY_COOKIE_SECURE = false
            OAUTH2_PROXY_SKIP_PROVIDER_BUTTON = true
            OAUTH2_PROXY_SET_XAUTHREQUEST = true
            OAUTH2_PROXY_SSL_INSECURE_SKIP_VERIFY = true
            OAUTH2_PROXY_SSL_UPSTREAM_INSECURE_SKIP_VERIFY = true
            OAUTH2_PROXY_WHITELIST_DOMAINS = ".${config.liquid_domain}"
            OAUTH2_PROXY_REVERSE_PROXY = true
            {{- range service "${upstream}" }}
            OAUTH2_PROXY_UPSTREAMS = "http://{{.Address}}:{{.Port}}"
            OAUTH2_PROXY_REDIRECT_URL = "http://{{.Address}}:{{.Port}}/oauth2/callback"
            OAUTH2_PROXY_REDEEM_URL = "http://{{.Address}}:{{.Port}}/o/token/"
            OAUTH2_PROXY_PROFILE_URL = "http://{{.Address}}:{{.Port}}/accounts/profile"
            {{- end }}
            {%- if extra_header %}
            LIQUID_ENABLE_HYPOTHESIS_HEADERS = true
            {%- endif %}
            LIQUID_DOMAIN = ${config.liquid_domain}
            LIQUID_HTTP_PROTOCOL = ${config.liquid_http_protocol}
            {{- range service "${upstream}" }}
            OAUTH2_PROXY_UPSTREAMS = "http://{{.Address}}:{{.Port}}"
            {{- end }}
          THREADS = ${threads}
          EOF
        destination = "local/docker.env"
        env = true
      }
      resources {
        network {
          mbits = 1
          port "authproxy" {}
        }
        memory = ${memory}
        cpu = 150
      }
      service {
        name = "${name}-authproxy"
        port = "authproxy"
        tags = [
          "traefik.enable=true",
          "traefik.frontend.rule=Host:${host}",
        ]
        // check {
        //   name = "ping"
        //   initial_status = "critical"
        //   type = "http"
        //   path = "/ping"
        //   interval = "2s"
        //   timeout = "1s"
        // }
        // check_restart {
        //   limit = 3
        //   grace = "55s"
        // }
      }
    }
  }
{%- endmacro %}

{%- macro set_pg_password_template(username) %}
  template {
    data = <<-EOF
    #!/bin/sh
    set -e
    sed -i 's/host all all all trust/host all all all md5/' $PGDATA/pg_hba.conf
    psql -U ${username} -c "ALTER USER ${username} password '$POSTGRES_PASSWORD'"
    echo "password set for postgresql host=$(hostname) user=${username}" >&2
    EOF
    destination = "local/set_pg_password.sh"
  }
{%- endmacro %}
