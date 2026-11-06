package main

import (
	"context"
	"embed"
	"io/fs"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"
)

//go:embed static/*
var assets embed.FS

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" { return value }
	return fallback
}

func main() {
	target, err := url.Parse(env("WORKER_URL", "http://127.0.0.1:8000"))
	if err != nil || target.Host == "" || (target.Scheme != "http" && target.Scheme != "https") {
		log.Fatal("Python服务地址无效，请检查WORKER_URL")
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.Transport = &http.Transport{ResponseHeaderTimeout: 60*time.Second, IdleConnTimeout: 90*time.Second}
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"detail":"研究计算服务暂不可用，请确认Python服务已启动"}`))
	}
	web, err := fs.Sub(assets,"static")
	if err != nil { log.Fatal("无法读取研究界面文件") }
	mux := http.NewServeMux()
	mux.Handle("/api/", http.MaxBytesHandler(proxy, 14<<20))
	mux.Handle("/", http.FileServer(http.FS(web)))
	server := &http.Server{Addr: env("PUBLIC_ADDR","127.0.0.1:8080"), Handler: mux,
		ReadHeaderTimeout: 10*time.Second, IdleTimeout: 90*time.Second}
	ctx, cancel := signal.NotifyContext(context.Background(),os.Interrupt,syscall.SIGTERM)
	defer cancel()
	go func() {
		<-ctx.Done()
		shutdown, stop := context.WithTimeout(context.Background(),10*time.Second)
		defer stop()
		_ = server.Shutdown(shutdown)
	}()
	log.Printf("个股研究界面已准备监听 %s，K线仅用于后台计算", server.Addr)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal("研究界面服务未能启动：",err)
	}
}
