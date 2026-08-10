# DevOps Ticket: 
Automate jemalloc 5.3 Installation for MariaDB Servers

## **📋 Summary**
Automate the compilation, installation, and configuration of **jemalloc 5.3.0** from source for MariaDB servers to improve memory management and reduce fragmentation in production database environments.

---

## **🔧 Technical Requirements**

### **1. Source Compilation Process**
```bash
# Download jemalloc 5.3.0 source
cd /usr/local/src
curl -L -o jemalloc-5.3.0.tar.bz2 \
  https://github.com/jemalloc/jemalloc/releases/download/5.3.0/jemalloc-5.3.0.tar.bz2

# Extract and compile
tar -xjf jemalloc-5.3.0.tar.bz2
cd jemalloc-5.3.0
./configure --prefix=/usr/local
make
make install
```

### **2. System Library Configuration**
```bash
# Update library cache
echo "/usr/local/lib" > /etc/ld.so.conf.d/jemalloc.conf
ldconfig

# Verify installation
ls -la /usr/local/lib/libjemalloc.so*
```

### **3. MariaDB Service Integration**
```bash
# Configure systemd service
echo 'Environment="LD_PRELOAD=/usr/local/lib/libjemalloc.so.2"' >> /usr/lib/systemd/system/mariadb.service

# Reload and restart
systemctl daemon-reload
systemctl restart mariadb
```

### **4. Service Restart and Validation**
```bash
# Manual restart required to load jemalloc
systemctl restart mariadb

# Verify service is running
systemctl status mariadb
```

```sql
-- Verify jemalloc is loaded by MariaDB
mysql -e "SHOW VARIABLES LIKE 'version_malloc_library';"

-- Expected output:
-- +------------------------+----------------------------------------------------------+
-- | Variable_name          | Value                                                    |
-- +------------------------+----------------------------------------------------------+
-- | version_malloc_library | jemalloc 5.3.0-0-g54eaed1d8b56b1aa528be3bdd1877e59c56fa90c |
-- +------------------------+----------------------------------------------------------+
```

**⚠️ Important Notes:**
- A **manual service restart** is required after jemalloc installation
- The `version_malloc_library` variable will only show jemalloc if properly loaded
- If the output shows system malloc, jemalloc is not loaded correctly