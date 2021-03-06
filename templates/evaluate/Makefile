SRC_DIR := $$(pwd | sed 's/\(.*\)MIS\/.*/\1MIS\/src/')

## Verbose
VERBOSE=0
ifeq ($(VERBOSE), 0)
	EXECUTOR = @
endif

## Debugging
DEBUG=0
ifeq ($(DEBUG), 1)
	EXECUTOR += /usr/bin/env ipython3 --pdb --
else
	EXECUTOR += /usr/bin/env python3 -u
endif

# Configs
CONFIG = './evaluation.yaml'
CKPT = 'ckpt.pt'
LOG_DIR = 'logs'
PREDI_DIR = 'outputs'

run: clean evaluate

predict:
	@echo "===== Evaluating ====="
	$(EXECUTOR) $(SRC_DIR)/evaluate.py --config $(CONFIG) --checkpoint $(CKPT) --log-dir $(LOG_DIR) --prediction-dir $(PREDI_DIR)

evaluate:
	@echo "===== Evaluating ====="
	$(EXECUTOR) $(SRC_DIR)/evaluate.py --config $(CONFIG) --checkpoint $(CKPT) --log-dir $(LOG_DIR)

log:
	@echo "===== Launch Tensorboard ====="
	tensorboard --logdir $(LOG_DIR) > /dev/null 2>&1

clean:
	rm -rvf $(LOG_DIR) $(PREDI_DIR)
