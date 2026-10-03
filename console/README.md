# AI Scientist yerel konsol

> **Güncel 0.46 field-lab:** konsol `127.0.0.1:8789`, API `127.0.0.1:8767`.
> Aşağıdaki varsayılan 8788/8766 adımları eski kuruluma aittir. Güncel
> başlatma ve ilk deney için [README](../README.md) kullanın.

Tarayıcı adresi: **http://127.0.0.1:8788**.

## Kullanım

- **Genel bakış:** M0 kabul kaydından güncel geçti/kısmi/açık sayıları, geliştirme ilerlemesi ve canlı host ölçümleri.
- **Kabul maddeleri:** 22 maddeyi duruma göre süzme, açıklama ve kayıtlı kanıtları inceleme.
- **Kontrolü çalıştır:** sınırlı bir CPU test grubunu gerçekten çalıştırır. Sonuç, çıkış kodu ve çıktı Sistem ekranında görünür. Bu kontrol GPU/model kabulü değildir ve kabul kaydını değiştirmez.
- **Deneyler:** yapılandırılmış Lab API üzerinden kayıtlı süitle başlatma, UUID ile izleme, durdurma ve doğrulanmış rapor okuma.
- **Sistem:** CPU yükü, kullanılabilir RAM/disk ve okunabiliyorsa NVIDIA GPU ölçümleri.

API bağlı değilken deney kontrolleri nedenini gösterir. Konsol bir veritabanı,
model, GPU broker veya AOS servisi başlatmaz. Mevcut AOS geliştirmesine müdahale etmez.

## Başlatma

Bu bilgisayardaki hazırlanmış tam Lab kurulumu için normal `cachyos`
kullanıcı terminalinde:

```sh
cd /home/cachyos/ai-scientist
bash ops/start-lab.sh
```

Bu komut mevcut DB, API, Director işçisi ve konsolu hazırlar. Önceden
Python 3.12 `.venv`, özel API/işçi ayarları, principal/suite kayıtları,
veritabanı konteyneri ve derlenmiş ön yüz bulunmalıdır. Temiz makine kurulumu
henüz sağlanmıyor. Konsol açılıp API bağlantısı hazır olduğunda CPU deneyleri
kullanılabilir; gerçek model çalışması ayrıca yapılandırma gerektirir.

Yalnız konsolu geliştirmek için proje kökünde mevcut `.venv` kurulmuş
olmalıdır. Ön yüz derlemesi için Node.js/npm gerekir; hazır derlemeyi açarken
bu derleme komutlarını tekrar çalıştırmak gerekmez:

```sh
cd console/web
npm ci
npm run build
cd ../..
bash ops/run-console.sh
```

Başlatıcı aynı porttaki başka bir servisi kapatmaz. Konsol kullanıcı systemd
servisinde 512 MiB RAM / yarım CPU sınırıyla çalışır; CPU kontrolü ayrı, tek bir
1 GiB / yarım CPU / 120 saniye sınırına sahip serviste çalışır. Her iki serviste
swap kapalıdır. Konsol otomatik olarak boot servisi kurulmasını gerektirmez.

`ops/run-console.sh` yalnız konsolu başlatır. Mevcut aktif birim varsa
ortam değişkenlerini yeniden uygulamaz; yüklü fakat kapalı birimi otomatik
başlatmaz. Tam yerel kurulum için yukarıdaki `ops/start-lab.sh` kullanılır.

Konsolun durumunu görmek için:

```sh
systemctl --user status swapp-ai-scientist-console.service
```

## Başlatma sorununu inceleme

Komutu masaüstü kullanıcı terminalinde, `sudo` olmadan çalıştırın. Docker
kapalıysa yalnız daemon için `sudo systemctl start docker` kullanın ve
başlatıcıyı normal kullanıcı olarak tekrar çalıştırın.

```sh
systemctl --user is-active swapp-ai-scientist-api.service swapp-ai-scientist-director-drain.service swapp-ai-scientist-console.service
curl --fail --silent --show-error http://127.0.0.1:8766/health
journalctl --user -u swapp-ai-scientist-api.service -u swapp-ai-scientist-director-drain.service -u swapp-ai-scientist-console.service -n 50 --no-pager
```

API sağlık yanıtı hazır olduğunu; konsoldaki API bağlantı durumu ise
principal/registry bağlantısının çalıştığını gösterir. Başlatıcı kimlik,
kaynak sınırı veya port çakışması bildirirse günlükleri inceleyin. Başka
servisi kapatmak veya veritabanını yeniden oluşturmak yerine mevcut kurulum
ayarını düzeltin. Günlük ve özel ortam dosyalarını herkese açık paylaşmayın.

Yerel API `127.0.0.1:8766`, konsol `127.0.0.1:8788`, mevcut DB
`127.0.0.1:55432` kullanır. Kullanıcı servisi adları ve DB adı bu kurulumun
tarihsel adlarıdır; SWAPP uygulamasının kurulu olması şart değildir.

Uzaktan erişim, bu makinedeki ayrı özel tünelin sorumluluğundadır.
`ops/start-lab.sh` tüneli başlatmaz. Genel çekirdek bir tünel hesabını,
kurumsal SWAPP endpoint'ini veya şirket verisini hazır olarak içermez;
bunlar kullanıcının özel fork/kurulumunda yapılandırılır.

## Mevcut Lab API'ye bağlama

Bağlantı değerleri başlatıcıya sunucu ortamı üzerinden verilir:

| Değişken | İçerik |
|---|---|
| `LAB_CONSOLE_API_URL` | Mevcut API'nin açık portlu loopback HTTP adresi |
| `LAB_CONSOLE_TOKEN_FILE` | Tam olarak bir `origin: local` principal içeren, 0600 izinli mevcut `lab-api-principals.v1` JSON dosyası |
| `LAB_CONSOLE_SUITE_REGISTRY_FILE` | Mevcut API'nin hash ile sabitlenmiş, private suite kayıt dosyası |
| `MODEL_RUNS_ENABLED` | Varsayılan `false`; konsoldaki gerçek model başlatma kontrolünü açar. Director GPU/model işlerini engelleyen bir kaynak kilidi değildir; sağlayıcı ve kaynak paylaşımı ayrıca yapılandırılır |

Token tarayıcıya aktarılmaz. AOS principal'ı yerel kullanıcı principal'ının
yerine kullanılamaz. Kayıtlı `fake-json` süitleri fixture testidir; gerçek model
sonucu olarak sunulmaz. API start işlemi bir koşuyu kuyruğa alır; gerçek yürütme
için mevcut Director dispatcher hizmeti de yapılandırılmış olmalıdır.

Suite dosyaları bu checkout'un `data/runtime` kökü altında doğrulanır. İzlenen
koşu durumları yalnız konsolun private kullanıcı durum dosyasında saklanır;
erişilemeyen API için önceki değerler eski durum olarak işaretlenir.

## Geliştirme ve sınırlar

UI `web/`, yerel sunucu bu dizindeki Python modüllerindedir. SWAPP frontend ve
canlı AOS dosyaları bu arayüz için değiştirilmez. Araştırma harness kimliği
konsol dosyalarından bağımsızdır. Çalışma zamanı haricî font/CDN kullanmaz.

Tasarım ve API sözleşmesi: `docs/ai-scientist/30-local-console-design.md`.
Test kanıtları ve görsel karşılaştırma: `docs/ai-scientist/31-local-console-review.md`.
Kabul kaynağı: `docs/ai-scientist/m0-acceptance.md`. Sayılar işin bitme yüzdesi
veya kalan süre tahmini olarak yorumlanmamalıdır.

## CPU baseline ve araştırma

**Yeni deney → İşlem → CPU baseline** kayıtlı veri üzerinde mevcut CPU baseline
matrisini çalıştırır. `/v1/baselines` kullanılır; öneri sayısı ve model token
bütçesi sıfırdır. `MODEL_RUNS_ENABLED=false` iken de kullanılabilir. Suite ve
süre sınırını seçip başlatın; koşu ayrıntısından durumunu izleyin, gerekirse
**Durdur** seçin, tamamlandığında **Rapor** açın. API ve Director dispatcher
ayrı olarak çalışıyor olmalıdır; CPU sandbox bağımlılıkları ve veri dosyaları
hazır olmalıdır. Süre bütçesi rapor hazırlama süresini de kapsar.

**Araştırma** mevcut sağlayıcı yolunu kullanır. `fake-json` hazır öneri fixture'ıdır;
`local-qwen` gerçek yerel model ve mevcut koordinasyon iznini gerektirir.
Baseline sonucu tam M0, gerçek LLM veya AOS birlikte çalışma kabulü değildir.
İşlem türü bu konsoldan başlatılan koşular için saklanır; dışarıdan UUID ile
izlenen ve türü bilinmeyen koşular açıkça böyle gösterilir.

## Geliştirme ilerlemesini güncelleme

`docs/ai-scientist/development-progress.json` yalnız yayımlanabilir kısa durum bilgisi
barındırır. Konsol bu dosyayı her genel bakış isteğinde okur; açık ekran 5 saniyede
bir yenilenir. Güncelleme kabul maddelerini veya deney durumlarını değiştirmez.

`updated_at` saat dilimli ISO tarihidir. `current_work`, `latest_result` ve
`next_step` en fazla 600 karakterdir. CPU/GPU durumları `pending`, `running`,
`passed`, `blocked` veya `quarantined` olabilir. Dosya 8 KiB sınırındadır;
eksik/geçersiz dosya güncelleme bekleniyor olarak görünür. 10 dakikadan eski kayıt
son güncelleme zamanı ile belirtilir. Güncellemeyi geçici dosyaya yazıp atomik
olarak değiştirin; token, DSN, özel veri veya ham oturum çıktısı eklemeyin.
