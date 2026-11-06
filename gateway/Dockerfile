FROM golang:1-alpine AS build
WORKDIR /src
COPY gateway /src
RUN CGO_ENABLED=0 go build -o /stock-research .
FROM alpine:3
RUN apk add --no-cache ca-certificates && adduser -D researcher
COPY --from=build /stock-research /usr/local/bin/stock-research
USER researcher
ENV PUBLIC_ADDR=0.0.0.0:8080
EXPOSE 8080
CMD ["stock-research"]
